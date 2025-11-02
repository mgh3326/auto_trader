# 블로그 글 모음

이 폴더에는 프로젝트 관련 블로그 글과 예제 코드가 포함되어 있습니다.

## 📝 글 목록

### 1. 한투 API로 실시간 주식 데이터 수집하기: AI 투자 분석의 시작
- **파일**: [blog_kis_api.md](blog_kis_api.md)
- **주제**: 한국투자증권 API를 사용한 국내 주식 실시간 데이터 수집 및 Google Gemini AI 분석
- **내용**:
  - KIS API 계정 설정 및 토큰 관리
  - 일봉/현재가 데이터 수집
  - 기술적 지표 계산 (MA, RSI, MACD, BB, Stoch)
  - AI 분석용 프롬프트 생성
  - 실제 삼성전자 분석 결과 (현재가 97,500원)

### 2. yfinance로 애플·테슬라 분석하기: 해외 주식 데이터 수집 완벽 가이드
- **파일**: [blog_yfinance.md](blog_yfinance.md)
- **주제**: yfinance를 사용한 해외 주식 데이터 수집 및 국내/해외 통합 전략
- **내용**:
  - yfinance vs KIS 해외주식 API 비교
  - 애플, 테슬라 등 미국 주식 데이터 수집
  - 국내/해외 데이터 구조 통일 전략
  - 1편 프롬프트 로직 재사용
  - 실제 애플(AAPL) 분석 결과 (현재가 $262.24)
  - 포트폴리오 통합 분석 전략

### 3. Upbit으로 비트코인 24시간 분석하기: 암호화폐 자동매매의 시작점
- **파일**: [blog_upbit.md](blog_upbit.md)
- **주제**: Upbit API를 활용한 암호화폐 24시간 자동 분석 시스템
- **내용**:
  - 왜 암호화폐 시장을 선택했는가 (24시간 개장, 빠른 피드백)
  - Upbit vs 다른 거래소 API 비교
  - JWT 인증 방식 및 API 사용법
  - 일봉/현재가/분봉 데이터 수집
  - 실제 비트코인 분석 결과 (현재가 163,725,000원)
  - WebSocket 실시간 시세 및 자동 주문
  - 국내주식/해외주식/암호화폐 통합 시스템

### 4. AI 분석 결과 DB에 저장하기: 비용 절감과 대시보드 구축
- **파일**: [blog_db_design.md](blog_db_design.md)
- **주제**: PostgreSQL 데이터베이스 설계로 AI API 비용 절감 및 대시보드 구축
- **내용**:
  - 문제점: 매번 AI API 호출 시 30초~1분 대기 + 비용 증가
  - 해결책: DB 저장으로 즉시 조회 (밀리초) + 90% 비용 절감
  - 정규화 설계: `stock_info` (마스터) ↔ `stock_analysis_results` (분석 결과)
  - Window Function으로 종목별 최신 분석 조회
  - 웹 대시보드: 통계 카드, 필터링, 히스토리 모달
  - 실제 API 엔드포인트 구현 (`/stock-latest`)

### 5. Python 프로젝트를 Poetry에서 UV로 마이그레이션하기: 10배 빠른 의존성 관리
- **파일**: [blog_uv_migration.md](blog_uv_migration.md)
- **주제**: Rust 기반 패키지 관리자 UV로 전환하여 개발 생산성 10배 향상
- **내용**:
  - Poetry의 문제점과 UV 선택 이유
  - UV 소개 및 Poetry 대비 장점 (10~100배 빠른 성능)
  - 마이그레이션 과정:
    - pyproject.toml 표준화 (PEP 621)
    - Dockerfile 최적화 (빌드 시간 81% 단축)
    - GitHub Actions CI/CD 개선 (71% 단축)
    - Makefile 업데이트
  - 발생한 이슈와 해결 방법
  - 성능 비교: 로컬/Docker/CI 모두 10배 이상 개선
  - 팀원 온보딩 가이드 및 실전 팁

## 🧪 예제 코드

### test_kis_blog_simple.py
독립 실행 가능한 KIS API + Gemini AI 예제

```bash
uv run python blog/test_kis_blog_simple.py
```

**주요 기능:**
- 삼성전자 데이터 수집
- 프롬프트 생성 및 출력
- Gemini AI 분석 요청
- DB 저장 없이 결과만 확인

### test_yahoo_blog.py
독립 실행 가능한 yfinance + Gemini AI 예제

```bash
uv run python blog/test_yahoo_blog.py
```

**주요 기능:**
- 애플(AAPL) 데이터 수집
- 해외 주식용 프롬프트 생성
- Gemini AI 분석 요청
- PER, PBR 등 펀더멘털 정보 포함

### test_upbit_blog.py
독립 실행 가능한 Upbit API + Gemini AI 예제

```bash
uv run python blog/test_upbit_blog.py
```

**주요 기능:**
- 비트코인 데이터 수집
- 암호화폐용 프롬프트 생성
- Gemini AI 분석 요청
- 24시간 변동률, 거래량 등 정보 포함

## 📚 블로그 완성 현황

### AI 자동매매 시리즈
- [x] **1편**: 한투 API로 실시간 주식 데이터 수집하기: AI 투자 분석의 시작
- [x] **2편**: yfinance로 애플·테슬라 분석하기: 해외 주식 데이터 수집 완벽 가이드
- [x] **3편**: Upbit으로 비트코인 24시간 분석하기: 암호화폐 자동매매의 시작점
- [x] **4편**: AI 분석 결과 DB에 저장하기: 비용 절감과 대시보드 구축

### 개발 인프라 개선
- [x] **5편**: Python 프로젝트를 Poetry에서 UV로 마이그레이션하기: 10배 빠른 의존성 관리

## 🔗 참고 링크

- [전체 프로젝트 GitHub](https://github.com/mgh3326/auto_trader)
- [KIS Developers](https://apiportal.koreainvestment.com/)
- [Upbit API 문서](https://docs.upbit.com)
- [yfinance 문서](https://pypi.org/project/yfinance/)
- [Google Gemini API](https://ai.google.dev/)
- [UV 공식 문서](https://github.com/astral-sh/uv)

## 📄 라이선스

이 프로젝트의 모든 블로그 글과 예제 코드는 MIT 라이선스를 따릅니다.
