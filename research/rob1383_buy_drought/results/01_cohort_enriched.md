# 01_cohort_enriched.csv — 방법

한 행 = 기각 코호트(`decision_bucket=deferred_no_action`) forecast 1건, 953행.

## 열 정의

| 열 | 정의 |
|---|---|
| `failed_gates_text` | 사유 원문(`contrary_evidence`)에서 **실패 방향** 문구만 정규식으로 잡은 게이트(`gates.parse_text_gates`). "통과/pass" 문구는 실패로 치지 않는다. 숫자 안의 소수점(39.83%)은 절 경계로 보지 않는다. |
| `failed_gates_recomputed` | 결정 시점 재구성 지지선(라이브 `get_support_resistance_impl`)을 정책 2026-09-30.1 로 판정. 통과에 가장 가까운 지지 레벨(실패 하위 게이트 수 최소 → 강도 높은 순 → 가까운 순)의 실패 하위 게이트. `deep_limit_band_shallow`(앵커가 −3% 보다 얕음) / `deep_limit_band_deep`(−12% 보다 깊음) / `no_support_below`(현재가 아래 레벨 없음). RSI 재구성값 > 45 면 `rsi_max`. 재구성 불가 행은 빈칸. |
| `failed_gates` | 두 출처의 합집합(재구성 deep 상세는 `deep_limit_band` 로 접음). 출처는 `failed_gates_source`(text / recomputed / text+recomputed / none). 둘이 다르면 두 열에 각각 남아 있다. |
| `text_recomputed_agree` | 지지 관련 게이트 집합의 **정확 일치** 여부(엄격). 부분 일치는 false. |
| `support_only_fail` | 지지 하위 게이트(strength·sources·within_pct·deep_limit_band) 중 하나 이상 실패 **AND** RSI ≤ 45 **AND** 원문이 지지 외 실패(upside·data_fail·기타)를 말하지 않음. RSI 는 원문에 수치가 있으면 원문값(세션이 실제 게이트에 쓴 값), 없으면 재구성값. |
| `support_only_basis` | `text` = 원문이 지지 실패를 직접 말함 / `recomputed_text_silent` = 원문이 어떤 게이트도 말하지 않고 재구성만 지지 실패(이때 upside 는 미검증) / `none`. |
| `parent_episode_id` | 아래 규칙. |
| `scored` | `status == closed` AND `outcome` not null. |

`other` 키워드(`other_keywords` 열): recovery_gate, liquidity, no_chasing, notional_band, thesis, averaging_ak, portfolio_cap, crash_day_hold.

## parent_episode 규칙 (파라미터)

같은 market+symbol 에서 시간순으로, 다음을 **모두** 만족하면 직전 기록의 에피소드에 합류, 아니면 새 에피소드:

- 직전 기록과의 간격 ≤ **5 거래세션**(KR/US, 종목 자체 일봉의 세션 수; 일봉 결측 시 ≤ 7 달력일) / crypto ≤ **72시간**
- 앵커가 에피소드 **첫 기록 앵커의 ±5%** 이내. 앵커 = 재구성상 통과에 가장 가까운 지지 레벨 가격, 없으면 forecast 목표가. 한쪽이 없으면 앵커 조건 생략.

ID = `market:symbol:첫기록UTC(YYYYmmddTHHMMZ)`. 규칙 문자열은 `episode_rule` 열에 행마다 기록.

## 근사·한계

- 원문 파싱은 정규식이다. 정밀도는 `q1_reconstruction_agreement.csv` 와 단위 테스트(원문 그대로의 픽스처)로만 검증했다.
- 2026-09-07 이전 기록은 정책상 지지 강도 하한이 `strong` 이었다(ROB-1351 로 `moderate`). 원문 판정은 당시 정책, 재구성 판정은 현행 정책이라 그 구간에서 출처 간 차이가 구조적으로 생긴다.
- v0 의 `labels` 는 참고용으로 `v0_labels` 열에 남겼다. "support" 단어만 나와도 붙는 라벨이라 지지 통과 행에도 붙는다 — 판정 근거로 쓰지 않는다.
