#!/bin/bash

# Auto Trader Docker 실행 스크립트

set -e

echo "🚀 Auto Trader Docker 실행 스크립트"
echo "=================================="

# .env 파일 확인
if [ ! -f ".env" ]; then
    echo "❌ .env 파일이 없습니다. env.example을 참고해서 .env 파일을 생성해주세요."
    exit 1
fi

# tmp 디렉토리 생성 (호스트에서)
echo "📁 tmp 디렉토리 생성 중..."
mkdir -p tmp logs
chmod 755 tmp logs

# Docker 이미지 빌드
echo "🔨 Docker 이미지 빌드 중..."
docker build -f Dockerfile.api -t auto_trader-api:local .

# 기존 컨테이너 정리 (선택적)
echo "🧹 기존 컨테이너 정리 중..."
docker rm -f auto_trader_api 2>/dev/null || true

# Docker 컨테이너 실행
echo "🏃 Docker 컨테이너 실행 중..."
docker run -d \
    --name auto_trader_api \
    --env-file .env \
    -p 8001:8000 \
    -v "$(pwd)/tmp:/app/tmp" \
    -v "$(pwd)/logs:/app/logs" \
    --restart unless-stopped \
    auto_trader-api:local

echo "✅ Auto Trader API가 실행되었습니다!"
echo "📍 API 주소: http://localhost:8001"
echo "📍 Health Check: http://localhost:8001/health"
echo ""
echo "📋 유용한 명령어:"
echo "  - 로그 확인: docker logs -f auto_trader_api"
echo "  - 컨테이너 중지: docker stop auto_trader_api"
echo "  - 컨테이너 제거: docker rm auto_trader_api"
echo ""
echo "🐳 Docker Compose 사용법:"
echo "  - DB/Redis만: docker compose up -d"
echo "  - API만: docker compose -f docker-compose.api.yml up -d"
echo "  - 전체 스택: docker compose -f docker-compose.full.yml up -d"
