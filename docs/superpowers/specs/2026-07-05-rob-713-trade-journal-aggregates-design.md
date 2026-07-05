# Design: ROB-713 — 저널 집계 서비스 (setup별 expectancy · R-multiple · MAE)

Date: 2026-07-05
Branch: rob-713
Linear: ROB-713 (학습 루프 3단계). Related: ROB-711 (decision_history, merged #1428), ROB-691 (JudgmentScoreboardPanel).
Parent design: `~/.gstack/projects/mgh3326-auto_trader/mgh3326-main-design-20260705-161950.md` §3단계.

## Problem

시중 트레이딩 저널(TradeZella/Edgewonk)의 표준 집계 — setup별 승률/expectancy/profit
factor, R-multiple, MAE(진입 후 최대 역행폭) — 이 시스템 어디에도 없다. provenance·Brier
등 원천은 시중 저널보다 깊은데 기본기가 빠진 역전된 갭. 기존 `expectancy` 코드는
research 백테스트 인제스트 전용(`research/nautilus_scalping/`, `research.backtest_runs`) —
실거래 경로 아님(리뷰 검증됨). 실거래 expectancy는 라이브 레저 체결에서 새로 계산해야 한다.

## Decisions (2026-07-05, 확정)

- **D1 — setup 태그 canonical 소스 = `strategy_key` 우선 + `intent` 폴백.** 기존 ROB-691
  성적표의 "strategy" 차원(`trade_retrospectives.strategy_key`, 세션 저작)과 일치. 없으면
  5값 `intent` enum 폴백, 둘 다 없으면 `untagged`. 웹/MCP 두 표면이 같은 태그 축을 공유.
- **D2 — 범위 = 백엔드 + MCP 먼저, ROB-691 웹 패널 확장은 fast-follow.** 설계 P2(주
  소비자는 LLM=MCP 응답 주입, 웹은 감사/보조). 성공 기준(오퍼레이터가 표를 인용)도 MCP 경로.
- **D3 — fill 소스 = provenance 있는 3 라이브 레저**(`review.trades` 아님). 태깅에
  `correlation_id`/`report_item_uuid`가 필요하고 decision_history `recent_fills`와 일관.

## Constraints

- **migration 0.** 새 테이블/컬럼 없음. 배치 아님 — 조회 시 계산 + in-process TTL 캐시.
- **ROB-501.** `app/**` in-process LLM 금지 — 전부 결정적 집계/read 코드. (정적 가드 기존 커버)
- **read-path 전용.** 주문 hot path 무접촉.
- **표본 수 명시.** n<10 태그는 `insufficient_sample` 라벨(과잉 해석 방지).

## Grounding (코드 사실, 2026-07-05 Explore)

- `investment_report_items` (`app/models/investment_reports.py:201`): `item_uuid`(레저가
  `report_item_uuid`로 참조), `intent`(enum: buy_review/sell_review/risk_review/
  trend_recovery_review/rebalance_review, `:234`), `evidence_snapshot` JSONB, `symbol`,
  `side`, `confidence`. **`correlation_id`/`strategy_key` 없음.**
- `evidence_snapshot["trade_setup"]` (`app/services/investment_reports/ingestion.py:48`):
  `{direction: long|short, stop, target, headline{entry,risk_pct,reward_pct,rr_ratio},
  legs[...]}`. 절대 risk/reward 필드 없음 — R-multiple 분모는 `|entry − stop|`로 계산.
  sell/exit은 R:R 없음. 서버 계산만(caller 공급 거부).
- 3 라이브 레저 (`app/models/review.py`): `KISLiveOrderLedger`(`:267`),
  `LiveOrderLedger`(`:358`, US+crypto), `TossLiveOrderLedger`(`:499`). 모두 `symbol`,
  `side`, `filled_qty`, `avg_fill_price`, `report_item_uuid`, **`correlation_id`(ROB-714,
  이 브랜치에서 추가)**, fees, `reconciled_at`/`trade_date`. live/toss는 realized PnL
  컬럼(`security_pnl_krw`, `total_pnl_krw`) 있음, kis_live(KR domestic)는 없음(journal 부기).
  단 FIFO엔 side/price/qty/ts만 필요 → 세 레저 모두 충분.
- `trade_retrospectives` (`app/models/review.py:979`): `strategy_key`(`:1050`, free text),
  `correlation_id`, `report_item_uuid`, `symbol`. ROB-691 성적표 `group_by=strategy`가
  `strategy_key or "no_strategy"`로 그룹핑(`trade_retrospective_service.py:752`).
- `review.trades` (`app/models/review.py:36`): per-fill(side/price/qty/fee/account/order_id),
  **round-trip 테이블 아님**. provenance id 없음 → 태깅 불가라 본 설계는 미사용.
- `get_ohlcv` (`app/services/market_data/service.py:589`):
  `async get_ohlcv(symbol, market, period, count, end=None) -> list[Candle]`. count≤200 cap.
  `Candle{timestamp, open, high, low, close, ...}` (`contracts.py:22`). MAE/MFE용.
- decision_history (`app/services/decision_history.py:66`):
  `build_decision_context(db, symbol, market, setup_tag=None)`. docstring `:75-76` —
  **"setup_tag is reserved for realized_r_by_tag (ROB-713 stage 3); unused here."** ← 주입 지점.
  주입 호출부: `analysis_tool_handlers.py:850` `_attach_decision_history`.
- MCP 등록 패턴: `forecast_registration.py:64` `register_forecast_tools` (2-file: tools +
  registration), read 집계는 `registry.py:196-197` **"Always" 블록**에 무조건 등록(DEFAULT
  프로파일 분기 아님).

## Architecture

신규 모듈 `app/services/trade_journal/aggregates.py` — 4 유닛, 각 독립 테스트 가능.

### Unit 1 — Fills → ClosedTrade (FIFO)

3 라이브 레저에서 체결 fill 행 로드(`filled_qty > 0`). `(account/broker, normalized
symbol)`로 그룹, 시간순 정렬, buy fill을 이후 sell fill과 FIFO 매칭 → `ClosedTrade`:

```
ClosedTrade{
  market, symbol, account,
  entry_price,   # 매칭된 매수 fill들의 수량가중 평균
  exit_price,    # 매도 fill avg_fill_price
  qty,           # 매칭(청산)된 수량
  entry_ts, exit_ts,
  pnl_abs, pnl_pct, fees,
  entry_item_uuids[], exit_item_uuid, entry_correlation_ids[], exit_correlation_id,
}
```

- 미청산 잔여(open residual) 제외. 부분 청산은 청산분만 카운트.
- long-only(buy→exit). 숏/옵션 out of scope.
- 심볼 정규화는 `app/core/symbol.to_db_symbol` 사용(레저 간 동일 심볼 통합).

### Unit 2 — Setup-tag 해소 (per ClosedTrade)

우선순위(D1):
1. **strategy_key(exact)**: exit/entry `correlation_id` → `trade_retrospectives.strategy_key`.
2. **strategy_key(symbol_window)**: exit_ts 기준 심볼+시간창 최신 retrospective.
3. **intent(exact)**: exit/entry `report_item_uuid` → `investment_report_items.intent`.
4. **intent(symbol_window)**: entry_ts 기준 심볼+시간창 최신 item.
5. **untagged**.

각 그룹에 `tag`(문자열), `tag_source ∈ {strategy_key, intent, untagged}`,
`link_quality ∈ {exact, symbol_window}` 기록. (link_quality는 decision_history 규약 재사용.)

### Unit 3 — Per-trade 메트릭

- **R-multiple**: `(exit_price − entry_price) / |entry_price − planned_stop|`.
  `planned_stop` = 링크된 item의 `evidence_snapshot.trade_setup.stop`(report_item_uuid
  exact → symbol_window 폴백). stop 없으면 R = null(해당 거래는 win_rate/expectancy_pct엔
  포함, R 집계에서 제외). 그룹은 `r_coverage`(R 계산된 거래 수/전체) 보고.
- **MAE/MFE**: `get_ohlcv(symbol, market, period="day", count, end=exit_ts)`로
  `[entry_ts, exit_ts]` 구간 일봉. `MAE = (min(low) − entry)/entry`(long 기준 가장 음수),
  `MFE = (max(high) − entry)/entry`. count는 거래일 기준 산정, ≤200 cap. 200일 초과 홀드는
  degrade + `mae_degraded` 라벨. 데이터 없으면 MAE/MFE null(거래는 나머지 집계 유지).

### Unit 4 — Per-tag 집계 (SetupAggregate)

```
SetupAggregate{
  tag, tag_source, link_quality,
  n, wins, losses, win_rate,
  expectancy_pct,        # mean(pnl_pct)
  expectancy_r,          # mean(R), R_coverage 거래만
  profit_factor,         # sum(win pnl_abs) / |sum(loss pnl_abs)|
  avg_r, median_r, r_coverage,
  avg_mae, avg_mfe, worst_mae,
  insufficient_sample,   # n < 10
}
```

## Exposure (이번 이슈)

### MCP `get_trading_scoreboard` (read-only)

- 2-file 패턴: `app/mcp_server/tooling/trading_scoreboard_tools.py` +
  `..._registration.py`. `registry.py` **"Always" 블록**(`:196` 부근)에 무조건 등록 —
  DEFAULT 프로파일 분기 아님(부수효과 없는 순수 read).
- 파라미터: `market?`, `account_mode?`, `date_from?`/`date_to?`(KST), `setup_tag?`(필터),
  `min_sample?`(기본 1). 반환: `{groups: [SetupAggregate...], overall: SetupAggregate,
  as_of, count}`. n<10 그룹도 반환하되 `insufficient_sample=true`.

### decision_history `realized_r_by_tag` 주입

- 예약된 훅 구현: `build_decision_context`가 심볼 이력에 등장하는 상위 ~3개 태그에 대해
  aggregates를 호출, `realized_r_by_tag: {tag: {n, expectancy_r, win_rate, profit_factor,
  avg_mae, insufficient_sample}}` 맵을 반환 dict에 추가. 페이로드 상한(태그 3개)으로 컨텍스트
  bound. `setup_tag` 인자가 주어지면 그 태그를 우선 포함.
- 호출부(`_attach_decision_history`)는 변경 최소 — build_decision_context 내부에서 처리.

## Caching

in-process TTL 캐시(테이블 없음 = migration 0):
- (a) MAE/MFE per ClosedTrade window: exit_ts 과거면 불변 → 긴 TTL(모듈 dict, key=
  (symbol, entry_ts, exit_ts)).
- (b) 집계 결과 per (market, filters): 짧은 TTL(~5분; reconcile로만 변함).
n이 작아 재계산 비용 낮음. 기존 on-demand 서비스의 모듈-레벨 dict+timestamp 패턴 따름.

## Out of scope → fast-follow

- ROB-691 `JudgmentScoreboardPanel` `group_by=setup` + expectancy/R/MAE 컬럼(프론트 칩/
  타입/API group_by). 별도 후속 이슈.
- 스냅샷 테이블 + 스케줄 빌더(= migration).
- 숏/옵션/인트라데이 MAE.

## Testing

- **Unit(aggregates)**: FIFO(부분체결, 다중 매수→단일 매도, open residual 제외), R
  계산(stop 있음/없음), expectancy/profit_factor 수식, MAE/MFE(합성 OHLCV), tag 해소
  우선순위(strategy_key→intent→untagged, exact vs symbol_window), `insufficient_sample`
  게이팅(n<10).
- **Fixtures**: 3 레저 fill + retrospective(strategy_key) + item(intent, trade_setup.stop)
  + stub OHLCV 시드. 기존 `tests/.../trade_journal/` 패턴 따름.
- **MCP**: `get_trading_scoreboard` 스모크(빈 데이터 → 빈 groups + overall n=0; 시드
  데이터 → n≥10 태그 1개 이상에서 expectancy 표 산출).
- **decision_history**: `realized_r_by_tag`가 태그 상한 3개로 bound되고 n<10 시
  insufficient_sample 라벨 확인.
- ROB-501 정적 가드 기존 커버(app/services 스캔).

## Success Criteria

n≥10인 태그 1개 이상에서 expectancy 표 산출 + `get_trading_scoreboard`/decision_history를
통해 오퍼레이터 세션이 이를 인용할 수 있는 경로 완성. (부모 설계 §Success Criteria 3단계.)

## Rollout / Distribution

기존 파이프라인(main → production, blue/green native). 신규 배포 채널·env 게이트 없음
(read-only, 부수효과 0). MCP 도구는 "Always" 등록이라 프로파일 플래그 불필요.
