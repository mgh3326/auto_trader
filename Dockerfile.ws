# syntax=docker/dockerfile:1.20

# ==============================================================================
# STAGE 1: 'builder' - 의존성 설치를 전담하는 스테이지
# This stage is dedicated to installing dependencies for the target architecture.
# ==============================================================================
FROM --platform=$TARGETPLATFORM python:3.13-slim AS builder

# UV 설치 (Install UV)
RUN pip install --upgrade pip && pip install uv

WORKDIR /app

# pyproject.toml, uv.lock, README.md 파일 복사 (hatchling build에 필요)
# Copy only the project metadata files first
COPY pyproject.toml uv.lock README.md ./

# UV 캐시를 활용하여 빌드 속도 향상
# Use build mount for caching to speed up subsequent builds
# 가장 무거운 작업인 의존성 설치를 이 단계에서 실행합니다.
# Execute the heaviest task, dependency installation, in this stage.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# ==============================================================================
# STAGE 2: 'final' - 최종 실행 이미지를 만드는 스테이지
# This stage builds the final, lean production image.
# ==============================================================================
FROM python:3.13-slim AS final

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
# 'builder' 스테이지에서 설치 완료된 .venv를 그대로 복사해옵니다.
# ✨ THE MOST IMPORTANT PART ✨
# Copy the installed packages from the 'builder' stage.
# UV installs dependencies in .venv so we copy that.
COPY --from=builder /app/.venv /app/.venv

# 앱 소스 코드 복사 (Copy application source code)
COPY . .

# 환경 변수 설정 (Set PATH for virtual environment)
ENV PATH="/app/.venv/bin:$PATH"

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

# 헬스체크 (Check if Python process is running)
HEALTHCHECK --interval=30s --timeout=3s --retries=5 \
    CMD pgrep -f "upbit_websocket_monitor.py" || exit 1

# WebSocket 모니터 실행 (Start the WebSocket monitor)
CMD ["python", "upbit_websocket_monitor.py"]
