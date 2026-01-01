# Tooling Migration Plan: Ruff + Pyright

## Overview

현재 분산된 린팅/포맷팅 도구들을 Ruff와 Pyright로 통합하여 단순화하고, CI에 누락된 lint 검사를 추가합니다.

### 현재 상태
| 도구 | 용도 | 설정 위치 |
|------|------|-----------|
| black | 포맷터 | pyproject.toml |
| isort | import 정렬 | pyproject.toml |
| flake8 | 린터 | Makefile 인라인 |
| mypy | 타입 체킹 | Makefile 인라인 |

### 목표 상태
| 도구 | 용도 | 설정 위치 |
|------|------|-----------|
| Ruff | 린터 + 포맷터 | pyproject.toml |
| Pyright | 타입 체킹 | pyproject.toml |

---

## Phase 1: Ruff 도입

### 1.1 작업 목록

- [x] Ruff 의존성 추가 (`uv add --group dev ruff`)
- [x] pyproject.toml에 `[tool.ruff]` 설정 추가
- [x] Makefile에 `lint-ruff`, `format-ruff` 명령어 추가 (기존 명령어 유지)
- [x] 기존 도구와 Ruff 결과 비교 테스트
- [x] 기존 lint 에러 수정 또는 ignore 설정

### 1.2 pyproject.toml 설정

```toml
[tool.ruff]
target-version = "py314"
line-length = 88
exclude = [
    ".venv",
    "alembic/versions",
    "__pycache__",
    "data/stocks_info",
    "data/coins_info",
]

[tool.ruff.lint]
select = [
    "E",      # pycodestyle errors
    "W",      # pycodestyle warnings
    "F",      # Pyflakes
    "I",      # isort
    "B",      # flake8-bugbear
    "C4",     # flake8-comprehensions
    "UP",     # pyupgrade
    "ARG",    # flake8-unused-arguments
    "SIM",    # flake8-simplify
]
ignore = [
    "E203",   # Whitespace before ':' (black 호환)
    "E501",   # Line too long (formatter가 처리)
    "W503",   # Line break before binary operator
]

[tool.ruff.lint.isort]
known-first-party = ["app"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
skip-magic-trailing-comma = false
line-ending = "auto"
```

### 1.3 Makefile 변경 (병행 기간)

```makefile
# 기존 명령어 유지 (Phase 4에서 제거)
lint: ## Run linting checks (legacy)
	uv run flake8 app/ tests/ --max-line-length=88 --extend-ignore=E203,W503
	uv run black --check app/ tests/
	uv run isort --check-only app/ tests/
	uv run mypy app/ --ignore-missing-imports

format: ## Format code (legacy)
	uv run black app/ tests/
	uv run isort app/ tests/

# 새 명령어 추가
lint-ruff: ## Run Ruff linting
	uv run ruff check app/ tests/

format-ruff: ## Format with Ruff
	uv run ruff format app/ tests/
	uv run ruff check --fix app/ tests/

lint-all: ## Run all linters (comparison)
	@echo "=== Ruff ===" && uv run ruff check app/ tests/ || true
	@echo "=== Legacy ===" && $(MAKE) lint || true
```

### 1.4 롤백 방법

```bash
# Ruff 제거
uv remove --group dev ruff

# pyproject.toml에서 [tool.ruff] 섹션 삭제
# Makefile에서 *-ruff 명령어 삭제
```

---

## Phase 2: Pyright 도입

### 2.1 작업 목록

- [x] Pyright 의존성 추가 (`uv add --group dev pyright`)
- [x] pyproject.toml에 `[tool.pyright]` 설정 추가
- [x] Makefile에 `typecheck-pyright` 명령어 추가
- [x] 기존 mypy와 Pyright 결과 비교 테스트
- [x] 타입 에러 수정 또는 ignore 설정

### 2.2 pyproject.toml 설정

```toml
[tool.pyright]
pythonVersion = "3.14"
pythonPlatform = "All"
typeCheckingMode = "basic"  # "off" | "basic" | "standard" | "strict" | "all"

include = ["app"]
exclude = [
    "**/__pycache__",
    ".venv",
    "alembic",
    "tests",
    "data",
]

# 점진적 마이그레이션을 위한 설정
reportMissingImports = "warning"
reportMissingTypeStubs = false
reportUnknownMemberType = false
reportUnknownParameterType = false
reportUnknownVariableType = false
reportUnknownArgumentType = false
```

### 2.3 Makefile 변경 (병행 기간)

```makefile
# 기존 mypy 명령어 유지
typecheck: ## Run mypy (legacy)
	uv run mypy app/ --ignore-missing-imports

# 새 명령어 추가
typecheck-pyright: ## Run Pyright
	uv run pyright app/

typecheck-all: ## Run all type checkers (comparison)
	@echo "=== Pyright ===" && uv run pyright app/ || true
	@echo "=== mypy ===" && uv run mypy app/ --ignore-missing-imports || true
```

### 2.4 롤백 방법

```bash
# Pyright 제거
uv remove --group dev pyright

# pyproject.toml에서 [tool.pyright] 섹션 삭제
# Makefile에서 *-pyright 명령어 삭제
```

---

## Phase 3: CI 개선

### 3.1 작업 목록

- [x] `.github/workflows/test.yml`에 lint job 추가
- [x] Ruff + Pyright를 CI에서 실행
- [x] lint 실패 시 PR 차단 설정

### 3.2 test.yml 변경사항

```yaml
name: Test

on:
  push:
    branches: [ main, develop ]
  pull_request:
    branches: [ main, develop ]

jobs:
  # 새로 추가되는 lint job
  lint:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4

    - name: Set up Python 3.14
      uses: actions/setup-python@v6
      with:
        python-version: "3.14"

    - name: Install UV
      run: pip install uv

    - name: Install dependencies
      run: uv sync --group dev

    - name: Run Ruff linter
      run: uv run ruff check app/ tests/

    - name: Run Ruff formatter check
      run: uv run ruff format --check app/ tests/

    - name: Run Pyright
      run: uv run pyright app/

  test:
    needs: lint  # lint 통과 후 테스트 실행
    runs-on: ubuntu-latest
    # ... 기존 test job 내용 유지 ...

  security:
    runs-on: ubuntu-latest
    # ... 기존 security job 내용 유지 ...
```

### 3.3 롤백 방법

```yaml
# lint job 삭제
# test job에서 needs: lint 제거
```

---

## Phase 4: 정리

### 4.1 작업 목록

- [x] 기존 도구 의존성 제거 (black, isort, flake8, mypy)
- [x] pyproject.toml에서 기존 설정 제거
- [x] Makefile 명령어 통합 (legacy 제거)
- [x] 문서 업데이트 (CLAUDE.md, README.md)

### 4.2 의존성 제거

```bash
uv remove --group dev black isort flake8 mypy
```

### 4.3 pyproject.toml 정리

**제거할 섹션:**
```toml
# 삭제
[tool.black]
line-length = 88

# 삭제
[tool.isort]
profile = "black"
line_length = 88
```

**최종 dev 의존성:**
```toml
[dependency-groups]
dev = [
    "ruff>=0.8.0",
    "pyright>=1.1.390",
    "bandit>=1.7.0,<2.0.0",
    "safety>=3.7.0,<3.8.0",
    "playwright>=1.56.0",
]
```

### 4.4 Makefile 최종 버전

```makefile
.PHONY: help install install-dev test lint format typecheck security clean dev

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	uv sync

install-dev: ## Install development dependencies
	uv sync --all-groups

test: ## Run all tests
	uv run pytest tests/ -v

test-unit: ## Run unit tests only
	uv run pytest tests/ -v -m "not integration"

test-integration: ## Run integration tests only
	uv run pytest tests/ -v -m "integration"

test-cov: ## Run tests with coverage report
	uv run pytest tests/ -v --cov=app --cov-report=html --cov-report=term-missing

test-fast: ## Run tests without coverage (faster)
	uv run pytest tests/ -v --no-cov

lint: ## Run linting checks (Ruff + Pyright)
	uv run ruff check app/ tests/
	uv run ruff format --check app/ tests/
	uv run pyright app/

format: ## Format code with Ruff
	uv run ruff format app/ tests/
	uv run ruff check --fix app/ tests/

typecheck: ## Run type checking with Pyright
	uv run pyright app/

security: ## Run security checks
	uv run bandit -r app/
	uv run safety check

clean: ## Clean up generated files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".coverage" -delete
	find . -type d -name "htmlcov" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +

dev: ## Start development server
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 4.5 CLAUDE.md 업데이트 내용

```markdown
### 코드 품질
\`\`\`bash
make lint                         # Ruff + Pyright 검사
make format                       # Ruff로 코드 포맷팅
make typecheck                    # Pyright 타입 체킹
make security                     # bandit, safety 보안 검사
\`\`\`
```

### 4.6 롤백 방법 (전체)

Phase 1-4 전체 롤백이 필요한 경우:

```bash
# 1. 기존 도구 재설치
uv add --group dev "black>=25.11.0,<25.12.0" "flake8>=7.3.0,<7.4.0" "isort>=7.0.0,<7.1.0" "mypy>=1.5.0,<2.0.0"

# 2. Ruff, Pyright 제거
uv remove --group dev ruff pyright

# 3. Git으로 설정 파일 복원
git checkout HEAD -- pyproject.toml Makefile .github/workflows/test.yml
```

---

## 마이그레이션 체크리스트

### Phase 1 완료 기준
- [x] `make lint-ruff` 성공
- [x] `make format-ruff` 성공
- [x] 기존 `make lint` 대비 동등하거나 더 나은 검출

### Phase 2 완료 기준
- [x] `make typecheck-pyright` 성공 (warning 허용)
- [x] 주요 타입 에러 없음

### Phase 3 완료 기준
- [x] CI lint job 추가됨
- [x] PR에서 lint 실패 시 머지 차단

### Phase 4 완료 기준
- [x] 기존 도구 완전 제거
- [x] `make lint && make format` 정상 동작
- [x] CI 정상 동작
- [x] 문서 업데이트 완료

---

## 참고 자료

- [Ruff Documentation](https://docs.astral.sh/ruff/)
- [Pyright Documentation](https://microsoft.github.io/pyright/)
- [ty - Pyright 후속 타입 체커](https://github.com/astral-sh/ty) (향후 전환 대비)
