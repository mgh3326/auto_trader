# Custom Metrics Usage Guide

이 문서는 비즈니스 로직에 추가된 커스텀 메트릭 사용 방법과 예시를 제공합니다.

## 목차

1. [개요](#개요)
2. [추가된 메트릭](#추가된-메트릭)
3. [SigNoz에서 메트릭 확인](#signoz에서-메트릭-확인)
4. [메트릭 쿼리 예시](#메트릭-쿼리-예시)
5. [대시보드 구성](#대시보드-구성)

## 개요

주요 비즈니스 로직(`service_analyzers.py` 및 `analysis_json.py`)에 다음과 같은 커스텀 메트릭이 추가되었습니다:

- ✅ 분석 실행 횟수 카운터
- ✅ 분석 소요 시간 히스토그램
- ✅ API 호출 성공/실패 카운터
- ✅ 코인/주식별 분석 태그
- ✅ 엔드포인트별 응답 시간
- ✅ 요청 상태 코드 분포

## 추가된 메트릭

### 1. Analysis Service Metrics (service_analyzers.py)

#### `analysis.executions` (Counter)
분석 실행 횟수를 추적합니다.

**Tags:**
- `status`: `success`, `failed`, `error`
- `asset_type`: `crypto`, `equity_us`, `equity_kr`
- `asset_name`: 코인/주식 이름 (예: "비트코인", "AAPL")
- `market`: `upbit`, `yahoo`, `kis`
- `model`: AI 모델 이름
- `decision`: `buy`, `hold`, `sell` (성공 시)
- `confidence_range`: `high`, `medium`, `low` (성공 시)

**사용 예시:**
```python
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()
await analyzer.analyze_coin_json("비트코인")

# 메트릭 자동 기록:
# analysis.executions{status="success", asset_type="crypto",
#   asset_name="비트코인", market="upbit", decision="buy",
#   confidence_range="high"} = 1
```

#### `analysis.duration` (Histogram)
분석 소요 시간을 밀리초 단위로 기록합니다.

**Tags:**
- `status`: `success`, `error`
- `asset_type`: `crypto`, `equity_us`, `equity_kr`
- `asset_name`: 코인/주식 이름
- `market`: `upbit`, `yahoo`, `kis`
- `model`: AI 모델 이름
- `decision`: `buy`, `hold`, `sell` (성공 시)
- `confidence_range`: `high`, `medium`, `low` (성공 시)

**사용 예시:**
```python
# 비트코인 분석 시작
await analyzer.analyze_coin_json("비트코인")

# 메트릭 자동 기록:
# analysis.duration{status="success", asset_type="crypto",
#   market="upbit"} = 2345.67 (ms)
```

#### `api.calls` (Counter)
외부 API 호출 횟수를 추적합니다.

**Tags:**
- `service`: `upbit`, `yahoo`, `kis`
- `operation`: `collect_data`
- `status`: `success`, `error`

**사용 예시:**
```python
# 데이터 수집 시 자동으로 기록됨
await analyzer.analyze_coin_json("비트코인")

# 메트릭 자동 기록:
# api.calls{service="upbit", operation="collect_data", status="success"} = 1
```

#### `api.call.duration` (Histogram)
외부 API 호출 소요 시간을 밀리초 단위로 기록합니다.

**Tags:**
- `service`: `upbit`, `yahoo`, `kis`
- `operation`: `collect_data`

**사용 예시:**
```python
# 데이터 수집 시 자동으로 기록됨
await analyzer.analyze_coin_json("비트코인")

# 메트릭 자동 기록:
# api.call.duration{service="upbit", operation="collect_data"} = 450.23 (ms)
```

### 2. Analysis API Metrics (analysis_json.py)

#### `analysis_api.requests` (Counter)
Analysis API 요청 횟수를 추적합니다.

**Tags:**
- `endpoint`: `/api/results`, `/api/detail/{id}`
- `status`: `success`, `error`
- `has_filters`: `true`, `false` (필터 사용 여부)
- `error_type`: 에러 타입 (에러 시)

**사용 예시:**
```bash
# API 호출
curl http://localhost:8000/analysis-json/api/results?decision=buy

# 메트릭 자동 기록:
# analysis_api.requests{endpoint="/api/results", status="success",
#   has_filters="true"} = 1
```

#### `analysis_api.duration` (Histogram)
Analysis API 요청 처리 시간을 밀리초 단위로 기록합니다.

**Tags:**
- `endpoint`: `/api/results`, `/api/detail/{id}`
- `status`: `success`, `error` (에러 시)

**사용 예시:**
```bash
# API 호출
curl http://localhost:8000/analysis-json/api/results

# 메트릭 자동 기록:
# analysis_api.duration{endpoint="/api/results"} = 145.32 (ms)
```

#### `analysis_api.db_query.duration` (Histogram)
데이터베이스 쿼리 소요 시간을 밀리초 단위로 기록합니다.

**Tags:**
- `operation`: `count`, `select`

**사용 예시:**
```bash
# API 호출 시 자동으로 기록됨
curl http://localhost:8000/analysis-json/api/results

# 메트릭 자동 기록:
# analysis_api.db_query.duration{operation="count"} = 23.45 (ms)
# analysis_api.db_query.duration{operation="select"} = 67.89 (ms)
```

## SigNoz에서 메트릭 확인

### 1. SigNoz 대시보드 접속

```bash
# SigNoz 실행 (이미 실행 중이 아닌 경우)
docker-compose -f docker-compose.monitoring.yml up -d

# 브라우저에서 접속
open http://localhost:3301
```

### 2. Metrics Explorer 사용

1. 왼쪽 메뉴에서 **Metrics** 클릭
2. Metric 선택창에서 원하는 메트릭 검색:
   - `analysis.executions`
   - `analysis.duration`
   - `api.calls`
   - `analysis_api.requests`

3. Tag 필터 적용:
   - `asset_type = "crypto"`
   - `market = "upbit"`
   - `decision = "buy"`

### 3. 분석 실행 후 메트릭 확인

```python
# 비트코인 분석 실행
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()
await analyzer.analyze_coin_json("비트코인")
await analyzer.analyze_coin_json("이더리움")
await analyzer.analyze_coin_json("리플")
```

**SigNoz에서 확인:**
1. Metrics → `analysis.executions`
2. Group By: `asset_name`
3. 결과: 비트코인, 이더리움, 리플 각각 1회 실행

## 메트릭 쿼리 예시

### 예시 1: 시간대별 분석 실행 횟수

```
sum(rate(analysis_executions[5m])) by (asset_type, market)
```

**결과:**
- `crypto, upbit`: 0.2 requests/sec (12 requests/min)
- `equity_us, yahoo`: 0.1 requests/sec (6 requests/min)

### 예시 2: 평균 분석 소요 시간

```
histogram_quantile(0.50, analysis_duration_bucket)
```

**결과:**
- P50 (중앙값): 2,500ms
- P95: 5,000ms
- P99: 8,000ms

### 예시 3: API 호출 성공률

```
sum(api_calls{status="success"}) / sum(api_calls) * 100
```

**결과:**
- 성공률: 98.5%

### 예시 4: 투자 결정 분포

```
sum(analysis_executions{status="success"}) by (decision)
```

**결과:**
- `buy`: 45%
- `hold`: 40%
- `sell`: 15%

### 예시 5: 신뢰도 분포

```
sum(analysis_executions{status="success"}) by (confidence_range)
```

**결과:**
- `high (>=70)`: 30%
- `medium (40-69)`: 50%
- `low (<40)`: 20%

## 대시보드 구성

### Dashboard 1: Analysis Overview

**Panels:**

1. **Total Analysis Executions** (Counter)
   ```
   sum(analysis_executions{status="success"})
   ```

2. **Analysis Success Rate** (Gauge)
   ```
   sum(analysis_executions{status="success"}) / sum(analysis_executions) * 100
   ```

3. **Average Analysis Duration** (Gauge)
   ```
   avg(analysis_duration)
   ```

4. **Analysis by Asset Type** (Pie Chart)
   ```
   sum(analysis_executions) by (asset_type)
   ```

5. **Analysis by Market** (Bar Chart)
   ```
   sum(analysis_executions) by (market)
   ```

6. **Decision Distribution** (Pie Chart)
   ```
   sum(analysis_executions{status="success"}) by (decision)
   ```

### Dashboard 2: Performance Metrics

**Panels:**

1. **Analysis Duration (P50, P95, P99)** (Line Chart)
   ```
   histogram_quantile(0.50, analysis_duration_bucket)
   histogram_quantile(0.95, analysis_duration_bucket)
   histogram_quantile(0.99, analysis_duration_bucket)
   ```

2. **API Call Duration by Service** (Line Chart)
   ```
   avg(api_call_duration) by (service)
   ```

3. **API Calls per Minute** (Line Chart)
   ```
   sum(rate(api_calls[1m])) by (service)
   ```

4. **Database Query Duration** (Line Chart)
   ```
   avg(analysis_api_db_query_duration) by (operation)
   ```

### Dashboard 3: Business Insights

**Panels:**

1. **Top Analyzed Assets** (Table)
   ```
   topk(10, sum(analysis_executions) by (asset_name))
   ```

2. **Confidence Distribution** (Bar Chart)
   ```
   sum(analysis_executions) by (confidence_range)
   ```

3. **Buy Signals by Market** (Bar Chart)
   ```
   sum(analysis_executions{decision="buy"}) by (market)
   ```

4. **Analysis Errors** (Line Chart)
   ```
   sum(rate(analysis_executions{status="error"}[5m])) by (error_type)
   ```

## 알림 설정 예시

### Alert 1: High Analysis Failure Rate

```yaml
alert: HighAnalysisFailureRate
expr: |
  sum(rate(analysis_executions{status="error"}[5m])) /
  sum(rate(analysis_executions[5m])) > 0.1
for: 5m
labels:
  severity: warning
annotations:
  summary: Analysis failure rate is above 10%
```

### Alert 2: Slow Analysis Performance

```yaml
alert: SlowAnalysisPerformance
expr: |
  histogram_quantile(0.95, analysis_duration_bucket) > 10000
for: 10m
labels:
  severity: warning
annotations:
  summary: P95 analysis duration is above 10 seconds
```

### Alert 3: API Call Failures

```yaml
alert: HighAPICallFailureRate
expr: |
  sum(rate(api_calls{status="error"}[5m])) /
  sum(rate(api_calls[5m])) > 0.05
for: 5m
labels:
  severity: critical
annotations:
  summary: API call failure rate is above 5%
```

## 실제 사용 시나리오

### 시나리오 1: 일일 분석 작업

```python
from app.analysis.service_analyzers import UpbitAnalyzer, YahooAnalyzer

# 암호화폐 분석
upbit = UpbitAnalyzer()
await upbit.analyze_coins_json(["비트코인", "이더리움", "리플"])

# 미국 주식 분석
yahoo = YahooAnalyzer()
await yahoo.analyze_stocks_json(["AAPL", "GOOGL", "MSFT"])
```

**기록되는 메트릭:**
- `analysis.executions`: 6회 (3 crypto + 3 equity_us)
- `analysis.duration`: 6개의 측정값
- `api.calls`: 6회 (각 분석마다 데이터 수집)
- `api.call.duration`: 6개의 측정값

### 시나리오 2: API를 통한 분석 결과 조회

```bash
# 필터링된 결과 조회
curl "http://localhost:8000/analysis-json/api/results?decision=buy&page=1&page_size=20"
```

**기록되는 메트릭:**
- `analysis_api.requests`: 1회
- `analysis_api.duration`: 1개의 측정값
- `analysis_api.db_query.duration`: 2개 (count + select)

### 시나리오 3: 에러 발생

```python
# 잘못된 코인명으로 분석 시도
await upbit.analyze_coin_json("존재하지않는코인")
```

**기록되는 메트릭:**
- `analysis.executions{status="failed", reason="symbol_not_found"}`: 1회

## 참고사항

- 모든 메트릭은 자동으로 기록되므로 별도의 코드 수정이 필요 없습니다
- 메트릭은 SigNoz에 실시간으로 전송됩니다 (배치 간격: 10초)
- 메트릭 보존 기간은 SigNoz 설정에 따라 다릅니다 (기본: 30일)
- 커스텀 대시보드는 SigNoz UI에서 생성 및 저장할 수 있습니다

## 추가 리소스

- [OpenTelemetry Metrics 문서](https://opentelemetry.io/docs/concepts/signals/metrics/)
- [SigNoz Metrics 가이드](https://signoz.io/docs/userguide/metrics/)
- [MONITORING_README.md](MONITORING_README.md) - 전체 모니터링 가이드
