# hk 3737 — US 지정가 깊이·분할 진입 반사실 shadow 설계

기준 코드 SHA `3a411fa2538ecce7074e7fa8ea78ff2dbb6be3da`. 이 문서는 설계이며 구현이나 실험 등록이 아니다. 목표는 ROB-1474의 같은 US 매수 에피소드에서 지정가 깊이와 분할 진입을 1분봉으로 비교하되 주문을 내지 않고 실제 체결과 구분하는 것이다. hk 3737의 2026-11-09 운영자 결정은 주문 없는 shadow만 허용하고 기존 시장가 실험은 유지한다. 선행 설계는 `docs/design/4154-candidate-to-retro-linking.md` §7, Q15–16이다. 아래 권고는 후속 구현 전에 확정할 연구 설계이지 운영 승인이나 성과 주장으로 보지 않는다.

## 1. 현재 입력과 한계

- `review.mock_strategy_orders`는 `strategy_id`, `venue`, `episode_id`, `symbol`, `side`, `qty`/`notional`, 상태와 `entry_price`, `meta`를 가진다(`app/models/mock_strategy.py:30`, `app/models/mock_strategy.py:46`). US 계획은 매수와 공매도를 만들고, 진입의 `episode_id`에 결정적 client id를 쓰며 `meta`에 신호일, horizon, 입력 해시, 이전 세션 종가 기반 `sizing_price`와 시장가 주문 종류를 기록한다(`app/services/mock_strategy/us.py:65`, `app/services/mock_strategy/us.py:102`, `app/services/mock_strategy/us.py:117`). 실제 계획에서 제외된 종목은 `skipped`이고 기록된 에피소드 모집단에 없다(`app/services/mock_strategy/us.py:76`, `app/services/mock_strategy/us.py:216`). 그러므로 이것은 원래 선별 후보 전체가 아니라 **기록된 매수 진입 에피소드**의 비교다. 이 모집단에서 누락된 선택 후보를 소급 생성하지 않는다.
- 선택 입력은 직전 XNYS 세션의 종가이며 snapshot cutoff가 그 신호일과 일치해야 한다(`app/services/mock_strategy/us_signals.py:69`, `app/services/mock_strategy/us_signals.py:78`, `app/services/mock_strategy/us_signals.py:99`). `prediction_at`과 `data_cutoff`는 연구 어휘지만 이 주문 행의 보장된 시각 컬럼이 아니다. 기존 하네스의 통계 라벨과 실행 라벨도 별개다(`docs/agent-contracts/market-data-and-screener-contracts.md:531`). 실제 관측 증거가 없는 시각은 만들어 넣지 않는다.
- 주문 journal의 상태와 `entry_price`는 브로커 체결 증거가 아니다. 대조에 쓰면 `basis=inference`와 journal 출처를 표시한다(`app/services/retro_v1/mock_experiments.py:1`). 이를 실제 브로커 체결이라고 부르지 않는다.
- US 1분봉 DB 동기화는 KIS·수동 보유 심볼의 합집합이고(`app/services/us_candles_sync_service.py:466`), 보존 기간은 90일이다(`alembic/versions/f8b6c4d2e1a3_add_us_candles_retention_policy.py:21`). 후보 전체의 재현 가능한 봉 저장소로 사용할 수 없다.
- 기존 paper 함수는 매수 `low <= limit`을 체결로 취급한다(`app/services/paper_fills.py:37`). 아래 규칙은 별도 연구 규칙이며 이 함수를 바꾸거나 그 결과와 같은 라벨을 쓰지 않는다.

## 2. 데이터 흐름과 경계

```text
읽기 전용 journal snapshot + 원본 입력 snapshot → 에피소드 입력 파일
                수동 GET → 불변 1분봉 snapshot → 오프라인 반사실 계산 → 연구 결과 파일
                journal 상태/가격 ───────────────────────────────→ 별도 대조 열
```

1. 후속 reader는 `venue=alpaca_paper`, ROB-1474 US `strategy_id`, `side=buy`, `meta.role=entry`의 기록된 행을 읽는다. `episode_id`로 진입과 같은 episode의 다른 행을 구분하되, 동일 식별자에 충돌하거나 진입이 여러 개면 임의로 하나를 택하지 않고 `unknown/ambiguous_episode`로 센다. journal은 읽기 전용이다. `decision_evidence` 행은 만들지 않는다(Q15).
2. 파일에는 `decision_ref`(episode를 내부적으로 식별하는 불투명 ref), `market=us`, `symbol`, `side`, `signal_session`, `prediction_at`, `data_cutoff`, `reference_price`, `reference_price_kind`, `planned_notional`, `currency`, `entry_shape_id`, `entry_levels`, `horizon_sessions`, `cost_model_id`, `fill_rule_id`, `real_outcome_ref`를 둔다. 원본 §7.3의 이름을 유지한다. decimal은 문자열, 시각은 시간대가 있는 UTC, 가격 종류와 출처를 함께 둔다. `qty`만 있는 경우에도 기록된 `meta.notional_per_name`을 계획 예산으로 쓰며 불일치·누락은 unknown이다. 기준 가격은 관측된 `meta.sizing_price`와 `previous_session_close`를 우선 사용한다. 사후 `entry_price`를 예측 시점 가격으로 쓰지 않는다.
3. `prediction_at`은 입력 snapshot의 결정 시각과 실제 관측 시각을 모두 증명할 수 있을 때 그 늦은 시각으로 둔다. `data_cutoff`는 실제 snapshot이 증명하는 마감이다. 시각·입력 해시·가격 종류를 연결할 수 없거나 prediction 이후에만 알 수 있는 값이면 그 episode는 계산에서 제외하고 사유를 계수한다. `opened_at`이나 주문 시각을 prediction으로 대체하지 않는다. 입력 snapshot과 읽은 journal 범위·버전·해시는 고정하고 개인 계좌번호, 브로커 주문번호, 원문 payload는 결과에 내보내지 않는다. 해시만으로 익명성이 보장되지는 않으므로 원본 및 매핑은 접근 통제된 로컬 보관소에 둔다.
4. `real_outcome_ref`는 journal 대조가 있을 때만 연결한다. 원본의 `entry_price`/상태는 반사실 결과와 별도 필드에 `basis=inference`로 둔다. 서로를 갱신하지 않으며 `execution_ledger`와 `review.*` 주문 테이블에 결과를 쓰지 않는다. 기존 reconcile/replay도 변경하지 않는다.

## 3. 수동 GET 1분봉 파일 생산자 제안

후속 티켓에서 별도 CLI를 만든다. 입력은 고정된 episode 파일의 US 심볼과 명시적인 UTC 시작·종료, feed, 가격 조정 방식이다. `scripts.mock_data_refresh`의 고정 GET, timeout, redirect 금지, 요청 속도 제한, bounded pagination과 실패 시 기존 snapshot 보존 패턴을 따른다(`scripts/mock_data_sources.py:56`, `scripts/mock_data_sources.py:138`, `scripts/mock_data_refresh.py:100`, `scripts/mock_data_refresh.py:107`). 기존 생산자는 일봉(`timeframe=1Day`)이므로 1분봉 파일을 이미 제공한다고 가정하지 않는다(`scripts/mock_data_sources.py:165`). 데이터 공급자는 Alpaca historical stock bars GET을 권고한다. IEX와 SIP는 다른 관측 범위이므로 feed를 고정해 manifest에 남기고 fallback하지 않는다. 인증은 데이터 전용 자격증명을 사용하고 값·응답 본문·계좌 정보는 기록하지 않는다. 운영자 승인 전 구현·호출하지 않는다.

- manifest는 스키마 버전, 공급자·endpoint 식별자, feed, 조정 방식, 조회 시각, 요청 범위, 심볼 집합과 입력 해시, XNYS calendar 버전, 봉 파일 SHA-256, 누락·오류 목록을 담는다. 봉은 `(symbol, UTC 시작시각, open, high, low, close, volume)`이며 가격은 정확한 decimal 문자열이다. 공급자 timestamp의 봉 시작 의미와 조정 방식은 구현 전 검증한다. 일봉 기준 가격과 분봉이 같은 가격 basis가 아니거나 기업행위 조정이 불명확하면 비교하지 않는다.
- 정규장만 `[open, close)`로 포함하고 휴일·조기 마감·DST는 XNYS calendar를 따른다. 중복 시각, 시간대 없음, 범위 밖 심볼, 비유한/비양수 OHLC, 모순된 high/low 또는 음수 거래량은 해당 입력을 invalid로 한다. 완성된 봉만 받는다. 데이터가 없는 분은 거래가 없었는지 공급 누락인지 봉만으로 증명하지 못하므로 0 거래나 미체결로 보간하지 않는다. 공급자 완전성 증거가 없으면 해당 창을 `unknown/data_gap`으로 표시한다.
- 임시 디렉터리에서 전 페이지를 검증·해시한 뒤 원자적으로 새 불변 세대를 공개한다. 오류·pagination 순환·제한 초과·부분 다운로드는 실패하며 기존 세대는 그대로 둔다. 같은 입력의 재실행은 이미 고정된 세대를 재사용하고 변경된 데이터는 새 해시·새 세대로 기록한다. reader는 세대와 해시를 고정한다. 보관·접근 권한과 공급자의 과거 데이터 가용성/라이선스는 실험 전 확인한다. 새 스케줄러나 자동 재시도는 없다.

## 4. 반사실 체결과 결과

실험 등록 및 manifest 봉인 전에 깊이·비율·관찰 창·비용 모델·기준군을 고정한다(`research/experiments/README.md:3`, `research/experiments/README.md:26`). 한 episode의 모든 형태는 같은 기준가·예산·prediction과 같은 봉 세대를 쓴다. 예를 들어 단일 얕은 limit과 여러 가격·예산 비율의 사다리를 비교할 수 있지만 수치는 이 문서에서 확정하지 않는다. 비율의 합은 1을 넘지 않으며 미사용 자금은 현금으로 남긴다. 지정가와 수량의 rounding·분할 가능 여부도 등록 시 고정한다.

- 가상 활성화 시각은 prediction 이후 명시한 시각이다. 이 시각보다 **엄격히 나중**에 시작하고 관찰 창 안에 완전히 포함된 정규장 봉만 시간순으로 본다. prediction/활성화가 봉 안에 있으면 그 봉은 제외한다. 모든 rung은 별도로 한 번만 판정한다.
- 매수 rung은 처음 `low < limit`인 봉에서만 `filled`, 가격은 limit으로 가정한다. `low == limit`만 관측되면 `touched_unfilled`이고 그 후 엄격 통과가 오면 최종 `filled`와 별도 `ever_touched`를 기록한다. 끝까지 접촉도 없고 완전한 창이 증명되면 `unfilled`다. 매도 대안이 나중에 등록되면 대칭으로 `high > limit`, 동등은 touch다(Q16). 저가가 limit보다 낮은 사실은 유동성·대기열·실제 체결을 증명하지 못한다.
- 같은 봉에서 여러 rung이 통과하면 봉 내 순서는 알 수 없다. 독립적 동시 활성 rung의 전량 가정만 허용하고 시간은 봉의 구간으로 기록한다. 이전 rung 체결 후 다음 rung을 활성화하는 순차 전략은 같은 봉에서 후속 rung 체결을 주장하지 않는다. 부분 체결, 호가, 슬리피지는 관측되지 않으며 전량 가정이라는 한계를 명시한다.
- 누락·비정상 봉, 불완전 창, 모호한 시각이나 가격 basis는 `unknown`이다. 유효한 엄격 통과 이전의 미확인 구간이 있으면 최초 체결 시점도 unknown이다. `touched_unfilled`, `filled`, `unfilled`, `unknown`의 최종 rung 건수와 episode 수를 분모와 함께 보인다. `ever_touched`는 별도 비독립 지표다. 시간 경과·가격 불명은 0이 아니다.
- 결과마다 `fill_basis=counterfactual_trade_through`, `fill_rule_id=counterfactual_trade_through_v1`, manifest·입력·봉 해시를 둔다. journal 대조값은 별도 출처·basis다. 비용은 등록된 추정 모델 id와 버전을 남기고 recorded 비용과 섞지 않는다. 관찰 봉만으로 시장가 실체결·실현손익 또는 전략 우위를 주장하지 않는다. 종료 가격, 비용, 비교 지표와 불완전 데이터 처리는 사전등록에서 명시하고, 출구 가격을 알 수 없으면 수익률은 unknown이다.

## 5. 결정과 권고

| 결정 | 권고 | 이유·미결 사항 |
|---|---|---|
| 모집단 | 기록된 ROB-1474 US 매수 진입 episode, `episode_id` 기준 | `skipped`와 공매도는 별도 계수; 후보 전체로 확대하지 않는다. |
| 기준 및 시각 | 기록된 이전 종가와 검증 가능한 snapshot 관측 시각 | 시각이나 가격 basis가 없으면 unknown. 과거 가격으로 누락 시각을 발명하지 않는다. |
| 봉 공급 | Alpaca GET, feed와 조정 방식 고정, 불변 파일 | IEX의 범위·SIP 권한·공급자 완전성 및 기업행위 basis는 구현 전 확인. |
| 체결 | 엄격 통과, limit 가격, 접촉 별도 | Q16 승인 규칙. 실제 체결이나 queue 모델이 아니다. |
| 형태·창·지표 | 실험 manifest에 사전 등록·봉인 | 숫자를 사후 결과에 맞춰 고르지 않는다. 통제군·비용·출구와 missing 처리도 봉인. |
| 해석 | 연구 진단만, journal과 별도 | 실제 체결·정책·주문 승격 금지. |

## 6. 후속 티켓 제안

각 티켓은 독립 PR이며 실행 전 운영자가 범위와 미결 결정을 확정한다. 사전등록은 테스트 데이터를 읽기 전에 완료한다. 모든 구현 PR은 관련 `docs/agent-contracts/`를 갱신하고 스케줄을 등록하지 않는다.

| 순서 | 산출물 | 필요한 행동 검증(후속 구현의 테스트 이름) |
|---|---|---|
| S1 | 연구 registry·manifest: 모집단, 형태, window, costs, 지표, 결측, 공급·가격 basis, 노출 및 look 규칙 봉인 | `test_manifest_pins_population_and_models`, `test_unsealed_or_changed_manifest_rejected` |
| S2 | 읽기 전용 episode export와 provenance 검증 | `test_entry_episode_only_and_skipped_counted`, `test_ambiguous_episode_unknown`, `test_missing_or_future_provenance_unknown`, `test_journal_outcome_is_inference`, `test_no_order_or_ledger_writes` |
| S3 | GET 1분봉 불변 파일 생산자와 reader | `test_fixed_get_pagination_and_feed`, `test_invalid_duplicate_or_gap_unknown`, `test_failed_fetch_preserves_generation`, `test_snapshot_hash_and_rerun`, `test_calendar_early_close_and_dst`, `test_price_basis_mismatch_unknown` |
| S4 | 순수 반사실 계산과 별도 결과 파일 | `test_buy_trade_through_touch_and_no_touch`, `test_sell_trade_through_and_touch`, `test_prediction_bar_excluded`, `test_ladder_same_bar_order_unknown`, `test_missing_window_is_unknown`, `test_labels_counts_and_cash_are_separate`, `test_replay_pinned_inputs` |
| S5 | 봉인된 manifest에 따른 수동 평가·보고 | `test_cost_and_outcome_provenance_separate`, `test_unknown_not_zero_and_denominators`, `test_registry_and_look_required` |

테스트 이름은 구현 계약의 제안이며 이 문서 PR에서 해당 기능을 구현하거나 테스트가 통과했다고 주장하지 않는다. 과거 행 소급, 브로커 호출·주문, 환경 게이트 활성화, 운영 스케줄과 ROB-1474 실행 경로 변경은 모든 티켓의 범위 밖이다.
