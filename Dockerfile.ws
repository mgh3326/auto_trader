# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 필수 OS 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# poetry 설치 및 의존성 설치
RUN pip install --upgrade pip && pip install poetry

# 프로젝트 메타데이터 복사
COPY pyproject.toml poetry.lock /app/
RUN poetry config virtualenvs.create false \
    && poetry install --only main --no-root --no-interaction --no-ansi

# 비루트 유저 생성
RUN useradd -u 10002 -m appuser

# 앱 소스 복사
COPY . .

# tmp 디렉토리 생성 및 권한 설정 (root 권한으로)
RUN mkdir -p /app/tmp && \
    chown -R appuser:appuser /app/tmp && \
    chmod 755 /app/tmp

# 전체 앱 디렉토리 권한 설정
RUN chown -R appuser:appuser /app

# 비루트 유저로 전환
USER appuser

# 볼륨 마운트 포인트 (선택적)
VOLUME ["/app/tmp"]

# WebSocket 모니터 실행
CMD ["python", "/app/upbit_websocket_monitor.py"]