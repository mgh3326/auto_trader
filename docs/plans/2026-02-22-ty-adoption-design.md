# Ty 즉시 전환 설계안 (Ruff + ty)

## Summary

- 목적: 타입체커를 `Pyright`에서 `ty`로 즉시 전환하고 품질 게이트를 `Ruff + ty`로 단일화한다.
- 범위: 타입체킹 대상은 `app/`만 유지한다.
- 정책: 병행/유예 없이 즉시 치환, CI 차단 게이트 즉시 적용.

## Locked Decisions

- 전환 방식: 즉시 치환
- 검사 범위: `app/`
- 엄격도: 기본 모드(전역 strict 미적용)
- 설정 위치: `pyproject.toml`
- 버전 정책: `ty>=0.0.18,<0.1.0`
- CI 정책: `ty` 실패 시 PR 차단
- 문서 정책:
  - 갱신: `README.md`, `TOOLING_MIGRATION_PLAN.md`, `AGENTS.md`, `CLAUDE.md`
  - 보존: `docs/plans/*`, `blog/*`

## Public Interfaces / Contract Changes

### Developer command interface

- `make lint`: `uv run pyright app/` -> `uv run ty check app/ --error-on-warning`
- `make typecheck`: `uv run pyright app/` -> `uv run ty check app/ --error-on-warning`

### CI interface

- `.github/workflows/test.yml`
  - `Run Pyright` -> `Run ty`
  - `uv run pyright app/` -> `uv run ty check app/ --error-on-warning`
- `.circleci/config.yml`
  - `Run Pyright type checker` -> `Run ty type checker`
  - `uv run pyright app/` -> `uv run ty check app/ --error-on-warning`

### Config contract

- `pyproject.toml`
  - remove `[tool.pyright]`
  - add:

```toml
[tool.ty.environment]
python-version = "3.13"

[tool.ty.src]
include = ["app"]

[tool.ty.rules]
all = "warn"
```

## Design Notes

- `ty` 공식 설정 스키마에서 `src`는 `[[...]]`가 아닌 단일 테이블(`[tool.ty.src]`)을 사용한다.
- `ty`는 `0.0.x` 베타 정책을 따르므로, `uv.lock` 기반 재현성을 필수로 유지한다.

## Implementation Plan

1. 사전 스캔: `pyright`, `[tool.pyright]`, `ty` 참조 위치 수집
2. `pyproject.toml` 전환
3. `uv lock` 갱신
4. Makefile 전환
5. CI 전환 (`.github/workflows/test.yml`, `.circleci/config.yml`)
6. 현재 기준 문서 동기화
7. 검증 실행
8. 단일 커밋으로 정리

## Test Cases & Scenarios

### 성공 시나리오

- `make lint`와 `make typecheck`가 모두 `ty` 기반으로 통과한다.
- CI lint 단계가 `ty` 스텝을 실행하고 성공한다.

### 실패 시나리오

- `uv run ty check app/ --error-on-warning` 실패 시 CI가 차단된다.
- 히스토리 문서 외 경로에서 `pyright` 잔존 참조가 발견되면 실패로 처리한다.

### 회귀 방지

- 타입체킹 스코프가 `app/`에서 `tests/`로 의도치 않게 확장되지 않았는지 확인한다.

## Verification Commands

```bash
uv run ruff check app/ tests/
uv run ruff format --check app/ tests/
uv run ty check app/ --error-on-warning
make lint
make typecheck
rg -n "\bpyright\b|\[tool\.pyright\]" --glob '!docs/plans/**' --glob '!blog/**'
```

## References

- https://docs.astral.sh/ty/reference/configuration/
- https://docs.astral.sh/ty/reference/cli/
- https://github.com/astral-sh/ty/releases/tag/0.0.18
- https://pypi.org/project/ty/
