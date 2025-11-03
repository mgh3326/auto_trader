# Repository Guidelines

## 프로젝트 구조 및 모듈 구성
핵심 애플리케이션 코드는 `app/`에 있으며, 분석 로직은 `analysis/`, 서비스 계층은 `services/`, FastAPI 라우터는 `routers/`에 나뉘어 있습니다. 백그라운드 작업과 Celery 태스크는 각각 `jobs/`와 `tasks/`에 위치합니다. 공통 설정과 데이터 모델은 `core/`와 `models/`에서 관리하며, 웹 대시보드 템플릿은 `templates/`에 저장됩니다. 데이터베이스 마이그레이션은 `alembic/`과 `alembic.ini`로 추적합니다. 배포·마이그레이션 스크립트는 `scripts/`에, 자동화된 점검 스크립트는 루트에 있는 `test_*.py`와 함께 유지됩니다. 기준 데이터와 글 자료는 `data/` 및 `blog/`에 정리되어 있습니다.

## 빌드·테스트·개발 명령어
`make install` 또는 `make install-dev`로 `uv` 기반 의존성을 동기화합니다. `make dev`는 `http://localhost:8000`에서 FastAPI 서버를 실행합니다. 전체 테스트는 `make test`, 단위/통합 테스트 분리는 `make test-unit`, `make test-integration`을 사용합니다. `make test-cov`는 `htmlcov/`에 HTML 커버리지를 생성합니다. 정적 검사와 포맷팅은 각각 `make lint`, `make format`으로 실행합니다. 데이터베이스 마이그레이션은 `uv run alembic upgrade head`, Docker 빌드와 실행은 `make docker-build`, `make docker-run`을 활용합니다.

## 코딩 스타일 및 네이밍 규칙
Python 3.11을 기본으로 하며, 4칸 공백 들여쓰기를 사용합니다. 서비스 경계를 설명하는 타입 힌트와 간단한 docstring을 유지해 주세요. 코드 포맷은 `black`(줄 길이 88)과 `isort`(Black 프로필)로 정렬하며, 임포트는 표준/서드파티/로컬 순서를 지킵니다. `flake8`에서는 E203, W503을 무시하므로 Black 기준의 슬라이싱 스타일을 따릅니다. PR 작성 전 `uv run mypy app/`을 실행해 타입 검사를 통과시켜 주세요. 파일·모듈은 snake_case, 클래스는 PascalCase, 상수는 UPPER_SNAKE_CASE 규칙을 따릅니다.

## 테스트 가이드라인
`pytest`는 엄격한 마커와 커버리지 기준(최소 70%)을 설정해 두었습니다. 테스트 함수는 `test_<모듈>` 형식, 테스트 클래스는 `Test<대상>` 네이밍을 사용합니다. 장시간 실행되는 케이스에는 `@pytest.mark.slow`, 통합 시나리오는 `@pytest.mark.integration`을 붙여 `pytest -m "not slow"`로 빠르게 제외할 수 있게 합니다. 커버리지를 확인하려면 `make test-cov`를 실행하고, 생성된 리포트는 필요할 때만 커밋합니다. 신규 단위 테스트는 `tests/`의 소스 미러 구조를 따라 배치해 주세요.

## 커밋 및 PR 가이드라인
커밋 메시지는 명령형(“Add …”, “Refactor …”)으로 작성하고 요약은 72자 내외로 유지합니다. 필요 시 `(#<이슈번호>)` 형태로 관련 이슈를 명시해 기존 기록과 일관성을 맞춰 주세요. 기능, 리팩터링, 포맷 변경을 하나의 커밋에 섞지 말고 논리적으로 분리합니다. PR에는 변경 요약, 테스트 계획(`make test` 결과나 로그), UI·API 변경 시 스크린샷 또는 예시 페이로드를 포함합니다. 로컬에서 린트와 테스트가 통과된 후 리뷰를 요청하세요.

## 환경 변수 및 보안
로컬 환경은 `env.example`(또는 프로덕션 유사 구성 시 `env.prod.example`)를 복사해 `.env`를 생성하고 값들을 채워 주세요. 업비트, KIS, Google, Redis, PostgreSQL 등 민감한 키는 `.env`에만 보관하고 버전에 포함시키지 않습니다. 새로운 설정을 추가할 경우 예제 파일과 `app/core/config.py`의 `pydantic-settings` 모델을 함께 업데이트해 문서와 코드의 동기화를 유지합니다.
