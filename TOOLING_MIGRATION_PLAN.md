# Tooling Migration Plan: Ruff + ty

## Overview

Python 코드 품질 게이트를 `Ruff + ty`로 단일화한다.

- 전환 방식: 기존 타입체커 제거 후 `ty` 즉시 치환 (병행 운영 없음)
- 타입체킹 범위: `app/` 유지
- CI 정책: 타입체킹 실패 시 PR 차단
- 문서 정책:
  - 갱신 대상: `README.md`, `TOOLING_MIGRATION_PLAN.md`, `AGENTS.md`, `CLAUDE.md`
  - 기록 보존: `docs/plans/*`, `blog/*`

## Locked Decisions

- 타입체커: `ty`
- 의존성 정책: `ty>=0.0.18,<0.1.0`
- Python 버전 기준: 3.13
- 엄격도: 기본 모드 (전역 strict 미적용)
- 설정 파일: `pyproject.toml`
- 규칙 기본값: `all = "warn"` (즉시 전환 시 기존 코드베이스와의 호환성 유지)

## Applied Contract Changes

### 1) Developer commands

- `make lint`
  - 변경 전: 기존 타입체커 호출
  - 변경 후: `uv run ty check app/`
- `make typecheck`
  - 변경 전: 기존 타입체커 호출
  - 변경 후: `uv run ty check app/`

### 2) CI commands

- GitHub Actions (`.github/workflows/test.yml`)
  - 기존 타입체커 스텝 -> `Run ty`
  - 기존 타입체커 명령 -> `uv run ty check app/`
- CircleCI (`.circleci/config.yml`)
  - 기존 타입체커 스텝 -> `Run ty type checker`
  - 기존 타입체커 명령 -> `uv run ty check app/`

### 3) Type checker configuration

- `pyproject.toml`
  - dev group dependency
    - 제거: 기존 타입체커 의존성
    - 추가: `ty>=0.0.18,<0.1.0`
  - 설정 블록
    - 제거: 기존 타입체커 설정 블록
    - 추가:

```toml
[tool.ty.environment]
python-version = "3.13"

[tool.ty.src]
include = ["app"]

[tool.ty.rules]
all = "warn"
```

## Why `[tool.ty.src]` (not `[[tool.ty.src]]`)

`ty` 공식 설정 스키마에서 `src`는 단일 테이블(`tool.ty.src`)이다.

- 참조: `https://docs.astral.sh/ty/reference/configuration/#src`

따라서 본 저장소는 공식 스키마를 기준으로 `[tool.ty.src]`를 사용한다.

## Verification Checklist

아래 명령을 모두 통과해야 전환 완료로 간주한다.

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/
make lint
make typecheck
rg -n "\bty\b|\[tool\.ty\]"
```

## Expected Residual References

아래 경로의 과거 타입체커 문자열은 기록 보존 목적이므로 허용한다.

- `docs/plans/*`
- `blog/*`

그 외 경로에서는 과거 타입체커 문자열 잔존을 허용하지 않는다.

## Rollback

즉시 복구가 필요할 경우:

1. `pyproject.toml`에서 `ty` 제거, 기존 타입체커 복원
2. `Makefile`/CI 스텝을 기존 타입체커 명령으로 복원
3. `uv lock` 재생성
4. `make lint && make typecheck`로 복구 확인

## References

- ty docs: `https://docs.astral.sh/ty/`
- ty config: `https://docs.astral.sh/ty/reference/configuration/`
- ty CLI: `https://docs.astral.sh/ty/reference/cli/`
- ty release 0.0.18: `https://github.com/astral-sh/ty/releases/tag/0.0.18`
