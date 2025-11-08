#!/bin/bash

# Auto Trader API Docker Compose 실행 스크립트

set -e

echo "🚀 Auto Trader API Docker Compose 실행"
echo "====================================="

# .env 파일 확인
if [ ! -f ".env" ]; then
    echo "❌ .env 파일이 없습니다. env.example을 참고해서 .env 파일을 생성해주세요."
    exit 1
fi

# tmp 디렉토리 생성 (호스트에서)
echo "📁 tmp 디렉토리 생성 중..."
mkdir -p tmp logs
chmod 755 tmp logs

# DB/Redis가 실행되고 있는지 확인
echo "🔍 DB/Redis 컨테이너 상태 확인 중..."
if ! docker ps --format "table {{.Names}}" | grep -q "auto_trader_pg\|auto_trader_redis"; then
    echo "⚠️  DB/Redis 컨테이너가 실행되지 않았습니다."
    echo "먼저 다음 명령어로 DB/Redis를 실행해주세요:"
    echo "  docker compose up -d"
    echo ""
    read -p "지금 DB/Redis를 실행하시겠습니까? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🗄️  DB/Redis 실행 중..."
        docker compose up -d
        echo "⏳ DB/Redis 초기화 대기 중..."
        sleep 10
    else
        echo "❌ DB/Redis 없이는 API를 실행할 수 없습니다."
        exit 1
    fi
fi

# API Docker Compose 실행
echo "🏃 API Docker Compose 실행 중..."
docker compose -f docker-compose.api.yml up -d --build

echo "✅ Auto Trader API가 실행되었습니다!"
echo "📍 API 주소: http://localhost:8001"
echo "📍 Health Check: http://localhost:8001/health"
echo ""
echo "📋 유용한 명령어:"
echo "  - 로그 확인: docker compose -f docker-compose.api.yml logs -f"
echo "  - API 중지: docker compose -f docker-compose.api.yml down"
echo "  - 전체 중지: docker compose down && docker compose -f docker-compose.api.yml down"


