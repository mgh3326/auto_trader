# Backtest Module Design

**Date:** 2026-03-22
**Status:** Approved

## Goal

`auto_trader` 레포에 Upbit 현물 일봉 데이터 기반의 미니멀 백테스트 모듈을 추가한다. 구조는 nunchi의 `auto-researchtrading` 패턴을 따르되, Upbit KRW 현물과 Phase 2 autoresearch 루프에 맞게 재현성과 고정 인터페이스를 우선한다.

## Scope

- 새 디렉토리 `backtest/` 추가
- Upbit 일봉 백필 스크립트 추가
- 고정 백테스트 엔진 추가
- 수정 가능한 유일한 전략 파일 추가
- 기준 전략 2종 추가
- autoresearch 지침서 추가
- `.gitignore` 업데이트
- Parquet 지원 의존성 추가

## Non-Goals

- 선물, 숏, 레버리지, 증거금 거래
- 분봉/틱 데이터 지원
- 라이브 스크리닝의 모든 외부 enrichment 완전 복제
- `backtest.py` 다기능 CLI 추가
- Phase 1에서의 자동 실험 루프 실행

## Constraints

- `prepare.py`와 `backtest.py`는 Phase 2 기준 고정 파일이다.
- `strategy.py`만 실험 루프에서 수정 가능해야 한다.
- 금액 기준은 모두 KRW다.
- 수수료는 Upbit 현물 0.05%를 양방향 반영한다.
- 슬리피지는 2bps를 양방향 반영한다.
- 엔진은 일봉 마감 기준으로만 동작한다.
- RPi5 환경을 고려해 pandas/numpy 중심으로 구현한다.

## File Layout

```text
backtest/
├── prepare.py
├── backtest.py
├── strategy.py
├── program.md
├── fetch_data.py
├── benchmarks/
│   ├── buy_and_hold.py
│   └── random_baseline.py
└── data/
```

## Architecture

### 1. Data Backfill

`backtest/fetch_data.py`는 Upbit REST API를 직접 호출해 KRW 마켓 일봉을 Parquet로 저장한다.

- 기본 수집 범위는 넓은 KRW universe다.
- 기본값은 `--top-n 100` 수준으로 잡는다.
- 실험 유니버스는 여기서 결정하지 않는다.
- 파일 단위는 심볼별 1개다. 예: `backtest/data/KRW-BTC.parquet`

수집은 현재 시점 snapshot으로 심볼 목록을 정하되, 실제 백테스트에 사용하는 심볼은 `prepare.py` 상수에서 고정한다. 이 분리는 Phase 2 점수 비교의 재현성을 위한 핵심 설계다.

### 2. Fixed Engine

`backtest/prepare.py`는 고정 백테스트 엔진이다.

- Parquet 로드
- split 기간 필터링
- 일자별 `BarData` 구성
- 전략 호출
- 주문 체결
- 포트폴리오 갱신
- 성과 지표 계산
- 점수 계산

전략별 동작 차이는 `strategy.py` 또는 benchmark 전략 파일에서만 발생해야 한다.

### 3. Fixed Entry Point

`backtest/backtest.py`는 단순 진입점이다.

- `Strategy` import
- `load_data("val")`
- `run_backtest()`
- `compute_score()`
- stdout 출력

벤치마크 선택용 CLI는 넣지 않는다. 이는 “고정 진입점” 제약을 지키기 위한 결정이다.

### 4. Strategy Contract

전략 계약은 아래로 고정한다.

```python
class Strategy:
    def on_bar(
        self,
        bar_data: dict[str, BarData],
        portfolio: PortfolioState,
    ) -> list[Signal]:
        ...
```

이 계약을 benchmark 전략도 동일하게 사용한다.

## Data Model

### Parquet Schema

모든 심볼 파일은 같은 스키마를 사용한다.

```text
date(str YYYY-MM-DD), open, high, low, close, volume, value
```

보장 조건:

- `date` 오름차순
- 날짜 중복 없음
- 결측 행 제거 또는 저장 전 정리

### Runtime Dataclasses

엔진은 아래 개념을 노출한다.

- `BarData`
- `Signal`
- `PortfolioState`
- `BacktestResult`

추가로 `PortfolioState`에는 전략 단순화를 위해 `position_dates: dict[str, str]`를 포함한다. 보유일 계산 책임을 엔진 쪽으로 옮겨 `strategy.py`를 가볍게 유지한다.

## Universe Policy

- 데이터 수집 universe: 넓게
- 실험 universe: 고정

`prepare.py`는 `DEFAULT_SYMBOLS` 상수만 사용한다. Parquet에 더 많은 종목이 있어도 기본 백테스트는 이 고정 리스트만 읽는다. 나중에 universe를 바꾸더라도 그것은 엔진 상수 변경으로만 일어나며, 전략 실험과 분리된다.

## Trading Rules

- Long-only
- 현금 부족 시 가능한 범위까지만 매수
- 음수 수량 금지
- 공매도 금지
- 레버리지 금지
- 시그널 생성과 체결은 같은 일자 종가 기준

체결 규칙:

- buy execution price = `close * (1 + slippage)`
- sell execution price = `close * (1 - slippage)`
- fee는 체결 금액 기준 별도 차감

`Signal.weight` 해석:

- `buy`: 총 equity 대비 목표 비중
- `sell`: 현재 보유 수량 대비 매도 비율

이 규칙은 DCA 전략과 benchmark 전략을 같은 인터페이스로 돌리기 위한 의도적 설계다.

## Performance Metrics

일별 equity curve 기반으로 계산한다.

- `total_return_pct`
- `sharpe`
- `max_drawdown_pct`
- `num_trades`
- `win_rate_pct`
- `profit_factor`
- `avg_holding_days`
- `backtest_seconds`

score는 Sharpe 중심이다.

```python
score = sharpe
if max_drawdown_pct > 20:
    score -= (max_drawdown_pct - 20) * 0.1
if num_trades < 10:
    score -= 1.0
```

## Strategy Fidelity

초기 `strategy.py`는 현재 라이브 크립토 스크리닝의 “RSI 코어”만 재현한다.

포함:

- RSI oversold 진입
- 동시 보유 수 제한
- 종목당 비중 제한
- 최소 보유 기간
- RSI overbought 또는 수익 실현 매도

제외:

- `market_warning`
- `is_safe_drop`
- 외부 API 기반 enrichment

이 필터들은 백테스트용 Parquet 데이터만으로는 재현성이 없고, Phase 1 목표는 autoresearch용 고정 엔진 구축이기 때문이다.

## Incremental Data Policy

기존 Parquet가 있을 때는 완전 재수집 대신 최근 구간 재수집 후 병합한다.

- 마지막 저장 날짜 근처의 최근 window를 다시 다운로드
- 기존 데이터와 concat
- `date` 기준 dedupe
- 다시 정렬 후 overwrite

이는 Upbit 일봉 수정, 타임존 경계, 마지막 봉 보정에 더 안전하다.

## Benchmarks

### buy_and_hold

- 첫 유효 거래일에 `DEFAULT_SYMBOLS` 균등 매수
- 이후 보유

### random_baseline

- 고정 seed 사용
- 낮은 빈도의 랜덤 매수/매도
- 비용 모델은 동일

둘 다 엔진 계약만 맞추고 별도 CLI는 두지 않는다.

## Testing Strategy

자동 테스트는 결정적 단위 테스트 위주로 간다.

- loader split 필터링
- 체결 수수료/슬리피지
- partial sell
- target-weight buy
- equity/MDD 계산
- RSI history 부족 구간 처리
- incremental merge dedupe

실제 Upbit 호출은 수동 검증으로 분리한다.

## Dependencies

현재 레포에는 Parquet 엔진 의존성이 보이지 않으므로 `pyarrow`를 추가한다.

## Risks

### 1. Period Coverage Mismatch

수집 가능한 Upbit 일봉 범위와 문서 상 split 기간이 어긋날 수 있다. 구현 시 실제 데이터 범위 확인 후 `prepare.py` 기간 상수를 조정해야 한다.

### 2. Overfitting by Fixed Validation Split

Phase 1에서는 `val` split 실행만 고정하지만, Phase 2 전에는 `train/val/test` 전체 운용 규칙을 다시 문서화해야 한다.

### 3. Strategy/Engine Boundary Creep

보유일 계산, 체결 비용, 성과 계산을 전략 파일로 넘기면 autoresearch 루프 안정성이 깨진다. 이 책임은 계속 `prepare.py`에 둔다.

## Approved Decisions

- `backtest.py`는 고정이며 benchmark CLI를 추가하지 않는다.
- 데이터는 더 넓게 수집하고, 백테스트 universe는 `prepare.py`에서 고정한다.
- `holding_days`용 진입일 기록은 엔진/포트폴리오 상태가 관리한다.
- Phase 1 초기 전략은 RSI 코어만 재현하고 `market_warning`, `safe_drop`는 제외한다.
