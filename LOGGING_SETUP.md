# Logging Setup Guide

이 문서는 Auto Trader의 기본 로깅 운영 기준을 설명합니다.

## 기본 원칙

- 애플리케이션 로깅은 Python `logging` 사용
- 프로세스 표준 실행 커맨드 사용 (래핑 없음)
- 운영 로그/에러는 컨테이너 로그 및 애플리케이션 로그로 확인

## 로그 레벨

`LOG_LEVEL` 환경 변수로 제어합니다.

```bash
LOG_LEVEL=INFO
```

권장 값:
- 개발: `DEBUG` 또는 `INFO`
- 운영: `INFO` 또는 `WARNING`

## 실행 예시

```bash
# API
uv run uvicorn app.main:api --reload --host 0.0.0.0 --port 8000

# Celery worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info

# MCP
uv run python -m app.mcp_server.main
```

## 운영 체크리스트

- 로그 파일/컨테이너 로그 접근 가능 여부 확인
- 예외 발생 시 Telegram 알림 수신 여부 확인
- 서비스 재기동 후 로그 정상 출력 여부 확인
