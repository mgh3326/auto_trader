#!/bin/bash

# setup-test-env.sh
# env.example을 기반으로 테스트용 환경 변수를 생성하는 스크립트

set -e

# env.example 파일 경로
ENV_EXAMPLE_FILE="env.example"

# env.example이 존재하는지 확인
if [ ! -f "$ENV_EXAMPLE_FILE" ]; then
    echo "Error: $ENV_EXAMPLE_FILE 파일을 찾을 수 없습니다."
    exit 1
fi

echo "테스트용 환경 변수를 설정합니다..."

# env.example에서 환경 변수를 읽어서 테스트용 값으로 설정
while IFS= read -r line; do
    # 주석이나 빈 줄 건너뛰기
    if [[ "$line" =~ ^[[:space:]]*# ]] || [[ -z "$line" ]] || [[ "$line" =~ ^[[:space:]]*$ ]]; then
        continue
    fi
    
    # KEY=value 형태의 라인 파싱
    if [[ "$line" =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)= ]]; then
        key="${BASH_REMATCH[1]}"
        
        # 테스트용 값 설정
        case "$key" in
            "DATABASE_URL")
                echo "DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test_db" >> $GITHUB_ENV
                ;;
            "REDIS_URL")
                echo "REDIS_URL=redis://localhost:6379/0" >> $GITHUB_ENV
                ;;
            "KIS_APP_KEY")
                echo "KIS_APP_KEY=DUMMY_KIS_APP_KEY" >> $GITHUB_ENV
                ;;
            "KIS_APP_SECRET")
                echo "KIS_APP_SECRET=DUMMY_KIS_APP_SECRET" >> $GITHUB_ENV
                ;;
            "TELEGRAM_TOKEN")
                echo "TELEGRAM_TOKEN=DUMMY_TELEGRAM_TOKEN" >> $GITHUB_ENV
                ;;
            "TELEGRAM_CHAT_IDS")
                echo "TELEGRAM_CHAT_IDS=123456789" >> $GITHUB_ENV
                ;;
            "GOOGLE_API_KEY")
                echo "GOOGLE_API_KEY=DUMMY_GOOGLE_API_KEY" >> $GITHUB_ENV
                ;;
            "GOOGLE_API_KEYS")
                echo "GOOGLE_API_KEYS=[\"DUMMY_GOOGLE_API_KEY_1\", \"DUMMY_GOOGLE_API_KEY_2\"]" >> $GITHUB_ENV
                ;;
            "OPENDART_API_KEY")
                echo "OPENDART_API_KEY=DUMMY_OPENDART_API_KEY" >> $GITHUB_ENV
                ;;
            "UPBIT_ACCESS_KEY")
                echo "UPBIT_ACCESS_KEY=DUMMY_UPBIT_ACCESS_KEY" >> $GITHUB_ENV
                ;;
            "UPBIT_SECRET_KEY")
                echo "UPBIT_SECRET_KEY=DUMMY_UPBIT_SECRET_KEY" >> $GITHUB_ENV
                ;;
            "UPBIT_BUY_AMOUNT")
                echo "UPBIT_BUY_AMOUNT=100000" >> $GITHUB_ENV
                ;;
            "UPBIT_MIN_KRW_BALANCE")
                echo "UPBIT_MIN_KRW_BALANCE=100000" >> $GITHUB_ENV
                ;;
            "TOP_N")
                echo "TOP_N=30" >> $GITHUB_ENV
                ;;
            "DROP_PCT")
                echo "DROP_PCT=-3.0" >> $GITHUB_ENV
                ;;
            "CRON")
                echo "CRON=0 * * * *" >> $GITHUB_ENV
                ;;
            "REDIS_MAX_CONNECTIONS")
                echo "REDIS_MAX_CONNECTIONS=10" >> $GITHUB_ENV
                ;;
            "REDIS_SOCKET_TIMEOUT")
                echo "REDIS_SOCKET_TIMEOUT=5" >> $GITHUB_ENV
                ;;
            "REDIS_SOCKET_CONNECT_TIMEOUT")
                echo "REDIS_SOCKET_CONNECT_TIMEOUT=5" >> $GITHUB_ENV
                ;;
            *)
                # 기타 변수들은 기본값이나 테스트용 값으로 설정
                echo "${key}=test_value" >> $GITHUB_ENV
                ;;
        esac
    fi
done < "$ENV_EXAMPLE_FILE"

# 필수 테스트 환경 변수 추가
echo "ENVIRONMENT=test" >> $GITHUB_ENV

echo "테스트용 환경 변수 설정이 완료되었습니다."

