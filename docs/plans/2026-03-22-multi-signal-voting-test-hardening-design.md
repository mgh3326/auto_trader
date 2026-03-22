# Multi-Signal Voting Test Hardening Design

**Date:** 2026-03-22
**Status:** Approved

## Goal

Phase 3 멀티시그널 투표 전략이 실제 요구사항을 계속 만족하는지 신뢰할 수 있도록 `tests/backtest/test_strategy.py`를 강화하고, `backtest/strategy.py`는 동작을 바꾸지 않는 범위에서만 경미하게 정리한다.

## Scope

- `tests/backtest/test_strategy.py`의 약한 assertion을 실질 검증으로 교체
- `backtest/strategy.py`의 비행동성 정리
- backtest 전용 검증 명령을 기준으로 회귀 확인

## Non-Goals

- 전략 성능 개선
- `prepare.py` 또는 `backtest.py` 수정
- 새 지표 추가 또는 threshold 재튜닝
- broad refactor

## Problem Summary

현재 Phase 3 구현은 테스트 통과 상태이지만, 핵심 투표 테스트 중 일부가 아래 이유로 회귀를 제대로 잡지 못한다.

- `bull_votes >= 0`, `bear_votes >= 0` 같은 자명한 assertion
- buy signal이 없어도 통과하는 조건부 reason 테스트
- vote threshold 경계값을 직접 검증하지 않음
- hard exit 우선순위가 bear-vote보다 앞선다는 점을 명시적으로 고정하지 않음

이 상태에서는 투표 로직이 부분적으로 깨져도 테스트가 녹색으로 남을 가능성이 있다.

## Recommended Approach

가장 안전한 방법은 테스트를 세 층으로 나누는 것이다.

1. 지표 helper 계약 테스트
2. `_evaluate_signals()`의 vote assembly 테스트
3. `on_bar()`의 의사결정 테스트

핵심 의사결정 테스트는 복잡한 시계열 패턴에 과도하게 의존하지 않고, 가능한 경우 `_evaluate_signals()`를 제어해 threshold 경계와 우선순위를 직접 검증한다. 이렇게 하면 fixture가 우연히 특정 vote 조합을 만들었는지 추정할 필요가 없어진다.

## Design Details

### 1. Indicator Helper Coverage

기존 helper 테스트는 유지하되, 반환 계약 중심으로만 둔다.

- 충분한 입력이면 값이 반환된다
- warmup 부족이면 `None`
- 필요 시 간단한 방향성만 확인한다

여기서는 정확한 trading decision을 검증하지 않는다.

### 2. Vote Assembly Coverage

`_evaluate_signals()` 테스트는 두 가지 목표만 가진다.

- 특정 fixture에서 특정 flag가 켜진다
- 대표적인 fixture에서 vote 수가 기대 범위 또는 기대 exact count를 만족한다

단, “0 이상” 같은 assertion은 금지한다. exact count가 안정적인 fixture는 exact count로 검증하고, 노이즈가 있는 경우에만 좁은 범위를 허용한다.

### 3. Decision Coverage

`on_bar()` 테스트는 실제 요구사항을 직접 고정한다.

- `bull_votes == MIN_VOTES - 1` 이면 buy 없음
- `bull_votes == MIN_VOTES` 이면 buy 발생
- `bear_votes == MIN_SELL_VOTES - 1` 이면 sell 없음
- `bear_votes == MIN_SELL_VOTES` 이면 sell 발생
- stop-loss, RSI recovery, holding-period exit가 bear-vote보다 우선
- buy/sell reason이 반드시 `"Bull votes "` 또는 `"Bear votes "` prefix를 가진다

이 부분은 `_evaluate_signals()`를 patch 해서 deterministic input을 만드는 방식을 우선한다.

### 4. Strategy Cleanup

`backtest/strategy.py` 변경은 동작 불변 원칙을 지킨다.

- 미사용 `_get_rsi()` 제거 여부 확인
- vote reason 생성 로직을 helper로 추출할지 검토
- helper docstring/반환 타입 명확화
- 함수 내부 `pandas` import를 모듈 상단으로 이동하는 수준의 정리

새 로직, 새 파라미터, 새 행동 변화는 추가하지 않는다.

## Verification

최소 검증은 아래로 고정한다.

```bash
uv run pytest tests/backtest/test_strategy.py -v
uv run pytest tests/backtest -v
```

데이터가 있으면 추가로 아래를 실행한다.

```bash
uv run backtest/backtest.py
```

단, 로컬에 `backtest/data`가 없으면 score 비교는 보류하고 pytest 기반 계약 검증만 완료 기준으로 본다.

## Risks And Mitigations

- Risk: overly synthetic tests that no longer reflect real history patterns
  - Mitigation: helper/vote assembly는 실제 history fixture를 유지하고, decision 경계만 patch 기반으로 분리
- Risk: cleanup 중 행동 변경
  - Mitigation: strategy 정리는 small-step으로 제한하고 매 단계마다 `tests/backtest/test_strategy.py`를 재실행
