# Logging Setup Guide

이 문서는 Auto Trader의 로그 운영 및 Sentry 연동 기준을 설명합니다.

## 기본 원칙

- 애플리케이션 로깅은 Python `logging` 사용
- Sentry는 `SENTRY_DSN`이 있을 때만 활성화
- `logger.error`는 이벤트로 수집 (`SENTRY_ENABLE_LOG_EVENTS=true`)
- 민감한 헤더/토큰 값은 전송 전 마스킹

## 로그 레벨

```bash
LOG_LEVEL=INFO
```

권장:
- 개발: `DEBUG` 또는 `INFO`
- 운영: `INFO` 또는 `WARNING`

## Sentry 환경 변수

```bash
SENTRY_DSN=
SENTRY_ENVIRONMENT=
SENTRY_RELEASE=
SENTRY_TRACES_SAMPLE_RATE=1.0
SENTRY_PROFILES_SAMPLE_RATE=1.0
SENTRY_SEND_DEFAULT_PII=true
SENTRY_ENABLE_LOG_EVENTS=true
```

## 실행 예시

```bash
uv run uvicorn app.main:api --reload --host 0.0.0.0 --port 8000
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info
uv run python -m app.mcp_server.main
```

## 운영 체크리스트

- `SENTRY_DSN`가 정확히 주입되었는지 확인
- 서비스별 `service` 태그로 이벤트 분리 확인
- 에러/트레이스/프로파일 유입 확인
- 민감 필드 마스킹 적용 여부 확인
