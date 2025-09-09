# syntax=docker/dockerfile:1.18

# ==============================================================================
# STAGE 1: 'builder' - 의존성 설치를 전담하는 스테이지
# This stage is dedicated to installing dependencies for the target architecture.
# ==============================================================================
FROM --platform=$BUILDPLATFORM python:3.11-slim AS builder

# Poetry 설치 (Install Poetry)
RUN pip install --upgrade pip && pip install poetry

WORKDIR /app

# pyproject.toml과 poetry.lock 파일만 먼저 복사
# Copy only the project metadata files first
COPY pyproject.toml poetry.lock ./

# Poetry 캐시를 활용하여 빌드 속도 향상
# Use build mount for caching to speed up subsequent builds
# 가장 무거운 작업인 의존성 설치를 이 단계에서 실행합니다.
# Execute the heaviest task, dependency installation, in this stage.
RUN --mount=type=cache,target=/root/.cache \
    poetry config virtualenvs.create false \
    && poetry install --only main --no-root --no-interaction --no-ansi

# ==============================================================================
# STAGE 2: 'final' - 최종 실행 이미지를 만드는 스테이지
# This stage builds the final, lean production image.
# ==============================================================================
FROM python:3.11-slim AS final

# 환경 변수 설정 (Set environment variables)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 필수 OS 패키지 설치 (Install essential OS packages)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 비루트 유저 생성 (Create a non-root user with UID 10002)
RUN useradd -u 10002 -m appuser

# ✨ 가장 중요한 부분 ✨
# 'builder' 스테이지에서 설치 완료된 패키지들을 그대로 복사해옵니다.
# ✨ THE MOST IMPORTANT PART ✨
# Copy the installed packages from the 'builder' stage.
# This avoids running 'poetry install' again.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# 앱 소스 코드 복사 (Copy application source code)
COPY . .

# tmp 디렉토리 생성 및 권한 설정 (Create and set permissions for the tmp directory)
RUN mkdir -p /app/tmp && \
    chown -R appuser:appuser /app/tmp && \
    chmod 755 /app/tmp

# 전체 앱 디렉토리 권한 설정 (Set permissions for the entire app directory)
RUN chown -R appuser:appuser /app

# 비루트 유저로 전환 (Switch to the non-root user)
USER appuser

# 볼륨 마운트 포인트 (Define volume mount point)
VOLUME ["/app/tmp"]

# WebSocket 모니터 실행 (Start the WebSocket monitor)
CMD ["python", "upbit_websocket_monitor.py"]
