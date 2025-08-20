# Auto Trader

자동 거래 시스템으로, 다양한 금융 데이터를 수집하고 분석하여 거래 신호를 제공합니다.

## 기능

- 주식 및 암호화폐 데이터 수집
- 기술적 분석 지표 계산
- **다중 시간대 분석 (일봉 + 분봉)**
  - 일봉 200개: 장기적 추세 분석
  - 60분 캔들 12개: 중기적 방향성 (하루 전반적 상승/하락 추세)
  - 5분 캔들 12개: 단기적 모멘텀과 변동성
  - 1분 캔들 10개: 초단기 급등락 및 거래량 폭증 포착
- 자동 거래 신호 생성
- Telegram 봇을 통한 알림
- 웹 대시보드

## 설치

### 요구사항

- Python 3.11+
- Poetry
- PostgreSQL
- Redis

### 설치 방법

1. 저장소 클론
```bash
git clone <repository-url>
cd auto_trader
```

2. 의존성 설치
```bash
poetry install
```

3. 환경 변수 설정
```bash
cp env.example .env
# .env 파일을 편집하여 필요한 설정값 입력
```

**필수 환경 변수:**
- `UPBIT_ACCESS_KEY`: 업비트 API 액세스 키
- `UPBIT_SECRET_KEY`: 업비트 API 시크릿 키
- `KIS_APP_KEY`: 한국투자증권 API 앱 키
- `KIS_APP_SECRET`: 한국투자증권 API 시크릿
- `GOOGLE_API_KEY`: Google Gemini API 키
- `TELEGRAM_TOKEN`: Telegram 봇 토큰
- `DATABASE_URL`: PostgreSQL 데이터베이스 연결 URL
- `REDIS_URL`: Redis 연결 URL

4. 데이터베이스 마이그레이션
```bash
poetry run alembic upgrade head
```

5. 애플리케이션 실행
```bash
poetry run uvicorn app.main:app --reload
```

## 사용법

### 암호화폐 분석 (업비트)

업비트 API를 사용하여 암호화폐를 분석합니다. 일봉 200개와 함께 다음 분봉 데이터를 자동으로 수집합니다:

- **60분 캔들 (최근 12개)**: 중기적 방향성 분석
- **5분 캔들 (최근 12개)**: 단기적 모멘텀 분석  
- **1분 캔들 (최근 10개)**: 초단기 변동성 분석

```python
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()
await analyzer.analyze_coins(["KRW-BTC", "KRW-ETH"])
```

### 주식 분석 (Yahoo Finance)

Yahoo Finance API를 사용하여 미국 주식을 분석합니다:

```python
from app.analysis.service_analyzers import YahooAnalyzer

analyzer = YahooAnalyzer()
await analyzer.analyze_stocks(["AAPL", "GOOGL", "MSFT"])
```

### 국내주식 분석 (KIS)

한국투자증권 API를 사용하여 국내 주식을 분석합니다:

```python
from app.analysis.service_analyzers import KISAnalyzer

analyzer = KISAnalyzer()
await analyzer.analyze_stock("삼성전자")
```

**참고**: KIS 분봉 데이터는 API 제한으로 인해 일부 상황에서 작동하지 않을 수 있습니다. 이 경우 일봉 데이터만으로 분석을 수행하며, 분봉 데이터 수집 실패 시에도 분석은 정상적으로 진행됩니다.

**KIS 분봉 API 제한사항**: 현재 KIS 분봉 API의 `time_unit` 파라미터가 제대로 작동하지 않아 모든 시간대에서 동일한 데이터가 반환됩니다. 이는 API 자체의 문제로, 향후 한국투자증권의 API 문서 업데이트나 기술지원을 통해 해결될 예정입니다.

## 테스트

### 테스트 환경 설정

개발 의존성 설치:
```bash
poetry install --with test
```

### 테스트 실행

모든 테스트 실행:
```bash
make test
# 또는
poetry run pytest tests/ -v
```

단위 테스트만 실행:
```bash
make test-unit
# 또는
poetry run pytest tests/ -v -m "not integration"
```

통합 테스트만 실행:
```bash
make test-integration
# 또는
poetry run pytest tests/ -v -m "integration"
```

커버리지 리포트와 함께 테스트 실행:
```bash
make test-cov
# 또는
poetry run pytest tests/ -v --cov=app --cov-report=html
```

### 테스트 마커

- `@pytest.mark.unit`: 단위 테스트
- `@pytest.mark.integration`: 통합 테스트
- `@pytest.mark.slow`: 느린 테스트 (선택적 실행)

### 코드 품질

코드 포맷팅:
```bash
make format
```

린팅 검사:
```bash
make lint
```

보안 검사:
```bash
make security
```

## 개발

### Makefile 명령어

```bash
make help          # 사용 가능한 명령어 목록
make install       # 프로덕션 의존성 설치
make install-dev   # 개발 의존성 설치
make test          # 모든 테스트 실행
make test-cov      # 커버리지와 함께 테스트 실행
make lint          # 코드 품질 검사
make format        # 코드 포맷팅
make clean         # 생성된 파일 정리
make dev           # 개발 서버 시작
```

### 테스트 구조

```
tests/
├── __init__.py
├── conftest.py           # 공통 fixture 및 설정
├── test_settings.py      # 테스트 환경 설정
├── test_config.py        # 설정 모듈 테스트
├── test_routers.py       # API 라우터 테스트
├── test_analysis.py      # 분석 모듈 테스트
├── test_services.py      # 서비스 모듈 테스트
└── test_integration.py   # 통합 테스트
```

## CI/CD

GitHub Actions를 통해 자동으로 다음을 실행합니다:

- **테스트**: Python 3.11, 3.12에서 테스트 실행
- **린팅**: flake8, black, isort, mypy 검사
- **보안**: bandit, safety 검사
- **커버리지**: 테스트 커버리지 리포트 생성

## 라이센스

이 프로젝트는 MIT 라이센스 하에 배포됩니다.
