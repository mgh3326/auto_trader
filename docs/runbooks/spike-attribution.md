# 일일 급등락 원인 귀속 (ROB-1303)

> 관측 전용. 브로커·주문·승인·워치 mutation 0, DB 쓰기 0, 스케줄러 등록 0.

## 1. 이게 뭔가

하루치 급등락 종목에 대해 **「무엇이 원인 후보인가」를 증거 링크와 함께** 남긴다.
새 데이터 소스를 만들지 않는다 — 이미 이 레포가 채우고 있는 세 재료를 **조립**한다.

| 재료 | 테이블 | 상태 |
|---|---|---|
| 뉴스 | `news_articles` + `news_article_related_symbols` (+ 판정 `symbol_news_relevance`) | 원인 후보 가능 |
| 공시 | `market_events` (source=`dart`, category=`disclosure`) | 원인 후보 가능 |
| 실적 | `market_events` (category=`earnings`) | 원인 후보 가능 |
| 수급 | `investor_flow_snapshots` | **v1 원인 후보 불가** (§4) |
| 섹터 | `symbol_sectors` + `kr_symbol_universe.sector_id` | **v1 원인 후보 불가** (§4) |

재료로 설명되지 않으면 `unattributed` 로 남는다. 🔴 **`unattributed` 를 "기타"·"시장
전반" 으로 분칠하지 않는다.** 모르는 것을 모르는 것으로 남기는 게 이 기록의 값이다.

## 2. 사전등록 (spec)

`app/services/spike_attribution/spec.py` 가 정본이며 `spec_sha256()` 로 봉인된다.
`tests/services/spike_attribution/test_spec_freeze.py` 가 핀을 검증하므로, 판정식·창·
표본 기준을 사후에 고쳐 읽으면 테스트가 red 가 된다.

- **급등락 정의**: 직전 종가 대비 **±5.0%**, `close_to_close` **또는** `intraday_extreme`
  중 하나만 넘어도 사건. 어느 basis 가 발화했는지 레코드에 남는다.
  (실제로 08-20 카카오는 종가 +3.48% / 장중 +5.35% 라 **장중 basis 로만** 잡혔다.)
- **증거 창**: `(직전 거래일 정규장 마감, 당일 정규장 마감]` — KR 15:30 KST / US 16:00 ET.
  창 뒤 문서는 `after_move` 로 **보이되 원인이 될 수 없다**. NXT(20:00)는 일봉 종가의
  근거가 아니므로 창에 넣지 않는다.
- **follow-through**: 앵커 = 급등일 종가, 기준 = 직전 종가.
  `retention_ratio = (창끝 종가 − 직전종가) / (급등일 종가 − 직전종가)`
  → `extended ≥1.0 > retained ≥0.5 > faded ≥0.0 > reversed`.
  창 = **3·10 거래일**, 봉 부족은 보간 없이 `unscorable`.
- **표본 하한**: 유형별 채점 가능 표본 **20건** 미만이면 유형 간 비교 자체를 금지한다
  (`cross_class_comparison_allowed=false`). 카운트만 보고한다.

## 3. 실행

```bash
export ENV_FILE=/path/to/.env.prod.native

# 지정 종목
uv run python -m scripts.attribute_daily_spikes --date 2026-08-20 \
    --symbol 035420 --symbol 035720 --created-by operator

# 그날 시장 전체 스캔
uv run python -m scripts.attribute_daily_spikes --date 2026-08-20 --all --limit 50

# follow-through 까지 (창이 찰 때까지는 unscorable 이 정상)
uv run python -m scripts.attribute_daily_spikes --date 2026-08-20 --all --score
```

MCP 로는 `get_spike_attribution(symbols, session_date, market, created_by)` — read-only.

**출력은 쓰지 않는다.** `prereg_forecast_save_kwargs` 는 `forecast_save` 인자 dict 일
뿐이며, 기록 여부는 세션·운영자가 정한다. CLI/도구는 `forecast_save` 를 호출하지 않는다
(`test_no_mutation_surface.py` 가 기계 검증).

## 4. 🔴 알고 감수하는 한계

읽는 사람이 이 기록을 실제보다 강하게 믿지 않도록, 한계를 먼저 적는다.

1. **수급·섹터는 v1 에서 원인이 될 수 없다.** 수급은 당일치가 T+1 이라 급등일에 없고,
   전일치는 *거래소가 언제 공표했는지* 를 이 레포가 기록하지 않아 사전 시각을 **발명해야만**
   창에 넣을 수 있다. 섹터 동조는 같은 세션에서 측정되므로 움직임을 *분류* 할 뿐 *선행* 하지
   않고, KR `sector_id` 커버리지도 부분(lazy-fill)이다. 둘 다 `material_availability` 에
   맥락으로만 남는다. → **운영자가 열거한 5유형 중 2개는 오늘 재료로 발화하지 않는다.**
   이 사실을 `unattributed` 로 흡수하지 않는다.
2. **`news` 유형은 운영자 열거 목록에 없던 추가분이다.** 증권사 리포트·언론 보도는
   공시로 뒷받침되지 않아 `disclosure` 로 넣으면 거짓이고, `unattributed` 로 버리면
   실재하는 링크를 잃는다. 그래서 6번째 유형으로 명시했다.
3. **시각을 추측하지 않는다.** `article_published_at` 은 naive 이고 그 naive 가 어느 tz
   인지는 인제스터마다 다르다. `materials.FEED_CLOCKS` 에 **확인된** 피드만 적격 시각을
   얻고, 나머지는 `timestamp_unknown` 으로 보이되 원인이 되지 못한다.
   - 확인됨: `http_naver_stock_aggregate` = KST exact (2026-08 발행시각 히스토그램).
   - 날짜만: `browser_naver_research_*`, `naver_item_news` (전 행 00:00:00).
   - **US 피드는 tz 미확인** → 현재 전부 부적격. US 경로는 "구조적으로 지원되나 미검증".
4. **KR 공시 심볼 연결은 회사명 정확일치다.** 이 DB 의 DART 행은 `market_events.symbol`
   이 **전부 NULL** 이라 `kr_symbol_universe.name` 과 정확일치로 잇는다. 개명·표기 차이는
   놓친다.
5. **공시 시각은 정규화 컬럼이 아니라 raw payload 파생이다.** ROB-128 normalizer 가
   `release_time_local` 을 버리므로 `raw_payload_json.rcept_dt` 를 읽는다. 레코드의
   `published_at_source` 에 그 출처가 적힌다.
6. **권리락·액면분할을 걸러내지 못한다.** 레포에 기업행위(코퍼레이트 액션) 캘린더가
   없다. 권리락 당일의 −30% 는 실제 급락이 아니지만 이 도구는 급락으로 잡고
   `unattributed` 로 끝난다. 유형별 채점 시 `unattributed` 코호트에 이 잡음이 섞여 있다.
7. **뉴스↔심볼 매핑 커버리지가 곧 귀속률의 상한이다.** `news_article_related_symbols`
   에 행이 없는 종목은 뉴스 후보가 0 이다. 그런 경우 `unattributed_reason` 이
   "조회됐으나 행 0 (커버리지 공백 가능)" 이라고 밝힌다 — 원인 부재와 구별해서 읽어라.
8. **후보 ≠ 인과.** 창 안에 있고 링크가 있다는 것뿐이다. 여러 후보는 여럿으로 남으며,
   `scored_class` 가 1순위를 고르는 것은 유형별 채점 분모를 고정하기 위한 **장부상 선택**
   이지 나머지를 기각했다는 뜻이 아니다.
9. **뉴스 판정 status 3값을 뭉개지 말 것** (ROB-491, `JUDGMENT_BY_STATUS`):
   | `symbol_news_relevance.status` | 우리 라벨 | 원인 후보 |
   |---|---|---|
   | `confirmed` | `judged_relevant` | ✅ (창 안이면) |
   | `pending` / 행 없음 | `unjudged` | ✅ (창 안이면) — **판정이 아니라 미판정** |
   | `excluded` | `judged_not_relevant` | ❌ eligibility=`judged_not_relevant` |
   `pending` 을 판정으로 읽으면 대기열 항목이 판정으로 둔갑하고, `excluded` 를 후보로
   올리면 외부 판정을 뒤집는다. `excluded` 행도 **레코드에는 남는다**(조용한 삭제 금지).
   status 를 쓰는 것은 언제나 외부 Job 이며 이 코드는 읽기만 한다.

## 5. 활용 훅 (배선만 — 자동 실행 0)

### ⓐ `momentum_spike` 의 `catalyst_basis`

`config/trading_policy.yaml` 의 `momentum_spike_profit_ladder.conditions.
required_thesis_evidence: [catalyst_basis, flow_basis]` 는 **코드가 아니라 세션이**
소비한다. `build_catalyst_basis()` 가 세션이 인용할 블록을 만든다.

🔴 이 훅은 **충족을 만들어내지 못한다**:
- `unattributed` → `satisfies_catalyst_basis_requirement=false` + 미충족 사유 그대로.
- `flow_basis` 는 **공급하지 않는다** (수급 T+1). 따라서
  `required_thesis_evidence_complete` 는 **항상 false** — catalyst_basis 가 채워졌다고
  해서 티어의 증거 쌍이 충족된 게 아니다.
- 라이브 게이트 문언·집행은 무접촉 (`can_loosen_live_gate=false`).

### ⓑ 유형별 follow-through 사전등록 채점

`build_prereg_forecasts()` 가 급등 1건당 창(3d/10d)마다 `forecast_save` 인자를 만든다.
귀속 레코드 전문(증거 링크 포함)이 `forecast_target.attribution_record` 로 함께 실린다
— **별도 테이블을 만들지 않은 이유**: `review.trade_forecasts` 가 이미 학습루프 척추이고
(CLAUDE.md), 마이그레이션 0 이 더 안전하다.

태깅: `cohort=spike_attribution`, `promote=false`, `calibration_exclude`,
`trade_performance_exclude`, `scoring_authority=rob-1303-spike-attribution.scoring`.
🔴 `forecast_resolve` 결과를 실험 점수로 쓰지 말 것 — 정본 채점은 `scoring.py` 다.

채점: `score_event()` → `aggregate_by_class()`. 표본 하한 20 미만이면 집계가
`cross_class_comparison_allowed=false` 를 돌려주고 유형 간 우열 선언을 막는다.

## 6. 금지

- 귀속 레코드 → 제안·주문·워치 승격 **0**.
- 채점 완료(표본 하한 충족) 전 중간값으로 정책·임계값 변경 논거 삼기 **금지**.
- 재료로 설명 안 되는 급등에 원인 붙이기 **금지**. 그 경우의 정답은 `unattributed`.
- 기존 뉴스 판정 파이프라인 규칙 변경 **금지** — auto_trader 코드는 기사를 자동 제외하지
  않는다 (ROB-491). 판정 행이 없으면 `unjudged` 후보로 **보인다**.
- 스케줄러(TaskIQ/cron/Prefect) 등록 **금지**. CLI/MCP 수동 호출만.
