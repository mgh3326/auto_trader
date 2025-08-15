# 분석 시스템 리팩토링 가이드

## 개요
기존의 `debug_upbit.py`, `debug_yahoo.py`, `debug_kis.py` 파일에서 중복되던 프롬프트 생성, Gemini 실행, DB 저장 로직을 공통 모듈로 분리하여 재사용 가능한 구조로 개선했습니다.

## 새로운 구조

### 1. 핵심 클래스들

#### `Analyzer` (app/analysis/analyzer.py)
- 프롬프트 생성, Gemini 실행, DB 저장을 담당하는 공통 클래스
- 스마트 재시도 로직 포함 (429 에러 시 모델 전환)
- 모든 서비스에서 공통으로 사용

#### `DataProcessor` (app/analysis/analyzer.py)
- 데이터 전처리를 담당하는 유틸리티 클래스
- 과거 데이터와 현재 데이터 병합 로직

#### 서비스별 분석기들 (app/analysis/service_analyzers.py)
- `UpbitAnalyzer`: 암호화폐 분석
- `YahooAnalyzer`: 미국주식 분석  
- `KISAnalyzer`: 국내주식 분석

### 2. 사용법

#### 개별 서비스 분석
```python
# Upbit 암호화폐 분석
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()
await analyzer.analyze_coins(["비트코인", "이더리움"])

# Yahoo Finance 주식 분석
from app.analysis.service_analyzers import YahooAnalyzer

analyzer = YahooAnalyzer()
await analyzer.analyze_stocks(["TSLA", "AAPL"])

# KIS 국내주식 분석
from app.analysis.service_analyzers import KISAnalyzer

analyzer = KISAnalyzer()
await analyzer.analyze_stocks(["삼성전자", "SK하이닉스"])
```

#### 통합 분석
```python
# 모든 서비스를 한 번에 실행
python debug_unified.py
```

### 3. 새로운 Debug 파일들

- `debug_upbit_new.py`: 리팩토링된 Upbit 분석기
- `debug_yahoo_new.py`: 리팩토링된 Yahoo 분석기  
- `debug_kis_new.py`: 리팩토링된 KIS 분석기
- `debug_unified.py`: 모든 서비스 통합 실행

### 4. 장점

1. **코드 중복 제거**: 공통 로직을 한 곳에서 관리
2. **유지보수성 향상**: 버그 수정이나 기능 추가 시 한 곳만 수정
3. **확장성**: 새로운 서비스 추가 시 간단한 상속만으로 구현 가능
4. **일관성**: 모든 서비스에서 동일한 에러 처리 및 재시도 로직
5. **테스트 용이성**: 공통 로직을 독립적으로 테스트 가능

### 5. 마이그레이션 가이드

기존 debug 파일들을 사용 중이라면:

1. 새로운 분석기 클래스들 import
2. 기존의 중복 코드를 분석기 메서드 호출로 교체
3. 필요에 따라 커스터마이징

### 6. 커스터마이징

특정 서비스에만 필요한 로직이 있다면:

```python
class CustomUpbitAnalyzer(UpbitAnalyzer):
    async def custom_analysis(self, coin_name: str):
        # 커스텀 로직 구현
        pass
```

## 파일 구조
```
app/analysis/
├── __init__.py              # 모듈 export
├── analyzer.py              # 핵심 분석기 클래스
├── service_analyzers.py     # 서비스별 분석기
├── prompt.py                # 프롬프트 생성 (기존)
└── indicators.py            # 기술적 지표 (기존)

debug_*.py                   # 새로운 debug 파일들
```
