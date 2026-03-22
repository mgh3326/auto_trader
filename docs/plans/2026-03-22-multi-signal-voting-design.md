# Multi-Signal Voting Design

**Date:** 2026-03-22
**Status:** Draft for approval

## Goal

`backtest/strategy.py`의 dual RSI 단일 진입 조건을 5~6개 불리언 시그널 기반 투표 시스템으로 확장해 validation score `+0.74`를 상회할 수 있는 탐색 기반을 만든다.

## Scope

- `backtest/strategy.py` 안에서만 지표 계산 및 투표 로직 추가
- `tests/backtest/test_strategy.py`를 현재 전략 계약에 맞게 정리하고 새 투표 동작 테스트 추가
- 기존 리스크 관리 상수 유지
- autoresearch가 튜닝할 수 있도록 새 파라미터를 상수로 분리

## Non-Goals

- `backtest/prepare.py`, `backtest/backtest.py` 수정
- 외부 TA 라이브러리 도입
- long/short 양방향 전략화
- 포트폴리오 sizing 또는 universe 정책 변경

## Constraints

- `Strategy.on_bar()` 시그니처는 유지한다.
- 모든 지표는 `bar.history`만으로 계산한다.
- numpy/pandas/stdlib만 사용한다.
- 기존 stop-loss 4.5%, cooldown 8일, RSI mean-reversion exit 46, position size 10%는 유지한다.
- autoresearch 루프 호환을 위해 상수 기반 파라미터화만 허용한다.

## Current State

현재 전략은 다음 순서로 동작한다.

1. 보유 포지션이면 stop-loss 우선
2. RSI 회복 + 수익 상태면 mean-reversion exit
3. 보유 기간 초과면 강제 청산
4. 비보유 포지션이면 dual RSI oversold에서만 진입

테스트 파일은 현재 구현과 어긋난 오래된 mock (`_get_rsi_from_history`)을 사용하고 있어, 이번 작업에서 정리하지 않으면 회귀 검증 신뢰도가 낮다.

## Candidate Approaches

### Option A: Pure buy-side voting, existing exits unchanged

- 매수만 `bull_votes >= MIN_VOTES`로 변경
- 매도는 현재 stop-loss / RSI exit / holding-period 그대로 유지

장점:

- 가장 안전하다
- 기존 `+0.74` edge를 덜 훼손한다
- 탐색 공간이 작다

단점:

- 멀티시그널 시스템 효과가 진입에만 제한된다
- sell-side 최적화 여지가 남는다

### Option B: Buy voting + optional bear-vote exit layered under current hard exits

- 매수는 `bull_votes >= MIN_VOTES`
- 매도는 stop-loss / RSI exit / holding-period를 그대로 최우선 유지
- 그 아래 보조 exit로 `bear_votes >= MIN_SELL_VOTES` 추가

장점:

- 요구사항과 nunchi 패턴을 더 직접적으로 반영한다
- 기존 하드 리스크 관리 장치를 보존한다
- 이후 autoresearch에서 buy/sell threshold를 모두 튜닝할 수 있다

단점:

- 파라미터 공간이 커진다
- 과도한 조기 청산으로 Sharpe가 악화될 수 있다

### Option C: Unified score-based entry/exit with weighted signals

- 불리언 투표 대신 각 시그널에 점수를 부여하고 순합 기준으로 매수/매도

장점:

- 유연성이 가장 높다

단점:

- 현재 요구사항보다 과하다
- 튜닝 복잡도가 급격히 커진다
- 이번 Phase 3의 “boolean voting” 요구와 어긋난다

## Recommendation

Option B를 권장한다.

- 요구사항의 `MIN_VOTES` / `MIN_SELL_VOTES` 구조를 그대로 반영할 수 있다.
- 기존 `+0.74`를 만든 hard exit들은 우선순위 그대로 유지할 수 있다.
- 이후 autoresearch에서 `MIN_SELL_VOTES`를 높게 두거나 사실상 비활성화하는 방향도 가능하다.

## Proposed Signal Set

초기 구현은 6개 시그널로 고정한다.

1. `rsi_oversold`: fast RSI와 slow RSI가 모두 `RSI_OVERSOLD` 이하
2. `macd_bull`: MACD histogram > 0
3. `bb_oversold`: 종가 < Bollinger lower band
4. `ema_bull`: fast EMA > slow EMA
5. `momentum_bull`: 종가 > N일 전 종가
6. `volume_spike`: 거래량 > 평균 거래량 * threshold

매도용 대응 시그널은 대칭적으로 둔다.

1. `rsi_recovered_or_hot`: 기존 RSI exit는 별도 hard exit로 유지, bear vote에는 `rsi_slow >= RSI_SELL_THRESHOLD` 같은 보조 조건 사용 가능
2. `macd_bear`: MACD histogram < 0
3. `bb_overbought`: 종가 > Bollinger upper band
4. `ema_bear`: fast EMA < slow EMA
5. `momentum_bear`: 종가 < N일 전 종가
6. `volume_fade_or_spike`: sell side에서는 거래량 조건을 기본 제외하거나 옵션화

초기 버전에서는 buy-side 6개, sell-side 5개 구성이 적절하다. 거래량은 진입 confirmation으로는 유용하지만 exit에서는 해석이 모호하므로 `bear_votes`에는 포함하지 않는 편이 단순하다.

## Data Flow

`on_bar()` 내부에서 심볼별로 아래 순서를 따른다.

1. `bar.history`에서 `close` / `volume` numpy 배열 추출
2. warmup 부족 여부 검사
3. RSI, EMA, MACD, Bollinger, momentum, volume 평균 계산
4. 불리언 시그널 dict 구성
5. `bull_votes` / `bear_votes` 계산
6. 보유 상태에 따라 sell branch 또는 buy branch 적용

## Decision Order

의사결정 우선순위는 다음으로 고정한다.

1. Stop-loss
2. Existing mean-reversion RSI exit (`rsi_slow >= RSI_EXIT and close > avg_price`)
3. Max holding days exit
4. Bear-vote exit
5. Cooldown check for re-entry
6. Bull-vote entry

이 순서는 기존 수익/손실 방어 로직을 보존하면서 투표 시스템을 진입/보조청산 레이어로 추가하기 위한 것이다.

## Parameter Surface

`strategy.py` 상수로 아래 값을 노출한다.

- `MIN_VOTES = 3`
- `MIN_SELL_VOTES = 2`
- `MACD_FAST = 12`
- `MACD_SLOW = 26`
- `MACD_SIGNAL = 9`
- `BB_PERIOD = 20`
- `BB_STD = 2.0`
- `EMA_FAST = 10`
- `EMA_SLOW = 30`
- `MOMENTUM_PERIOD = 10`
- `VOLUME_LOOKBACK = 20`
- `VOLUME_THRESHOLD = 1.5`
- 필요 시 `RSI_SELL_THRESHOLD` 같은 보조 매도 상수

모든 상수는 파일 상단에 모아 autoresearch 루프가 숫자만 바꿔 실험할 수 있게 한다.

## Testing Strategy

`tests/backtest/test_strategy.py`는 다음 축으로 재구성한다.

- 순수 지표 함수 테스트: RSI/EMA/MACD/Bollinger edge case
- buy vote 테스트: 임계값 이상일 때만 buy
- sell vote 테스트: hard exit 우선순위와 bear-vote 보조 exit 검증
- cooldown / max positions / insufficient history 회귀 테스트

Mock 기반 테스트는 최소화하고, 가능하면 직접 만든 `history` DataFrame으로 신호를 유도한다.

## Risks

- 단기 과매도 전략에 trend-following 시그널을 섞으면 진입 빈도가 과도하게 줄 수 있다.
- sell vote를 너무 공격적으로 켜면 기존 mean-reversion 수익 구간을 잘라낼 수 있다.
- 거래량 시그널은 일봉 crypto에서 노이즈가 클 수 있다.

대응:

- 초기값은 `MIN_VOTES=3`, `MIN_SELL_VOTES=2`로 완화한다.
- bear vote는 hard exit 아래에 배치한다.
- 백테스트와 pytest를 함께 돌려 기능 회귀와 성능 회귀를 분리해 확인한다.
