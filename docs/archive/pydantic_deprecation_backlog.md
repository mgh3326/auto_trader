# Pydantic Deprecation Backlog

## 목적
- TaskIQ 마이그레이션 PR과 분리하여, 비기능성 경고 정리를 별도 트랙으로 관리한다.
- 런타임 동작 변경 없이 경고 감축만 목표로 한다.

## 대상 파일
- `app/analysis/models.py`
  - `Field(..., enum=...)` 사용 제거 (`json_schema_extra`로 전환)
  - `max_items`를 `max_length`로 전환
- `app/auth/schemas.py`
  - class-based `Config`를 `ConfigDict`로 전환
- `app/routers/symbol_settings.py`
  - class-based `Config`를 `ConfigDict`로 전환

## 권장 작업 순서
1. `app/analysis/models.py` 필드 선언 정리
2. `app/auth/schemas.py` 모델 Config 마이그레이션
3. `app/routers/symbol_settings.py` 모델 Config 마이그레이션
4. 각 단계마다 `make lint && make test` 수행

## 완료 기준
- 테스트/린트 결과는 기존과 동일하게 green.
- 위 3개 파일 관련 Pydantic deprecation warning이 감소했음을 CI 로그로 확인.
- API 응답 스키마/필드 동작 변경 없음.
