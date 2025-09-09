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

# 출력 대상 결정 (GitHub Actions에서는 $GITHUB_ENV, 로컬에서는 .env.test)
if [ -n "$GITHUB_ENV" ]; then
    OUTPUT_TARGET="$GITHUB_ENV"
    echo "GitHub Actions 환경에서 실행 중..."
else
    OUTPUT_TARGET=".env.test"
    echo "로컬 환경에서 실행 중... .env.test 파일을 생성합니다."
    # 기존 .env.test 파일 초기화
    > "$OUTPUT_TARGET"
fi

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
                echo "DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test_db" >> "$OUTPUT_TARGET"
                ;;
            "REDIS_URL")
                echo "REDIS_URL=redis://localhost:6379/0" >> "$OUTPUT_TARGET"
                ;;
            "KIS_APP_KEY")
                echo "KIS_APP_KEY=DUMMY_KIS_APP_KEY" >> "$OUTPUT_TARGET"
                ;;
            "KIS_APP_SECRET")
                echo "KIS_APP_SECRET=DUMMY_KIS_APP_SECRET" >> "$OUTPUT_TARGET"
                ;;
            "TELEGRAM_TOKEN")
                echo "TELEGRAM_TOKEN=DUMMY_TELEGRAM_TOKEN" >> "$OUTPUT_TARGET"
                ;;
            "TELEGRAM_CHAT_IDS_STR")
                echo "TELEGRAM_CHAT_IDS_STR=123456789,987654321" >> "$OUTPUT_TARGET"
                ;;
            "GOOGLE_API_KEY")
                echo "GOOGLE_API_KEY=DUMMY_GOOGLE_API_KEY" >> "$OUTPUT_TARGET"
                ;;
            "GOOGLE_API_KEYS")
                echo "GOOGLE_API_KEYS=DUMMY_GOOGLE_API_KEY_1,DUMMY_GOOGLE_API_KEY_2" >> "$OUTPUT_TARGET"
                ;;
            "OPENDART_API_KEY")
                echo "OPENDART_API_KEY=DUMMY_OPENDART_API_KEY" >> "$OUTPUT_TARGET"
                ;;
            "UPBIT_ACCESS_KEY")
                echo "UPBIT_ACCESS_KEY=DUMMY_UPBIT_ACCESS_KEY" >> "$OUTPUT_TARGET"
                ;;
            "UPBIT_SECRET_KEY")
                echo "UPBIT_SECRET_KEY=DUMMY_UPBIT_SECRET_KEY" >> "$OUTPUT_TARGET"
                ;;
            "UPBIT_BUY_AMOUNT")
                echo "UPBIT_BUY_AMOUNT=100000" >> "$OUTPUT_TARGET"
                ;;
            "UPBIT_MIN_KRW_BALANCE")
                echo "UPBIT_MIN_KRW_BALANCE=100000" >> "$OUTPUT_TARGET"
                ;;
            "TOP_N")
                echo "TOP_N=30" >> "$OUTPUT_TARGET"
                ;;
            "DROP_PCT")
                echo "DROP_PCT=-3.0" >> "$OUTPUT_TARGET"
                ;;
            "CRON")
                echo "CRON=0 * * * *" >> "$OUTPUT_TARGET"
                ;;
            "REDIS_MAX_CONNECTIONS")
                echo "REDIS_MAX_CONNECTIONS=10" >> "$OUTPUT_TARGET"
                ;;
            "REDIS_SOCKET_TIMEOUT")
                echo "REDIS_SOCKET_TIMEOUT=5" >> "$OUTPUT_TARGET"
                ;;
            "REDIS_SOCKET_CONNECT_TIMEOUT")
                echo "REDIS_SOCKET_CONNECT_TIMEOUT=5" >> "$OUTPUT_TARGET"
                ;;
            *)
                # 기타 변수들은 기본값이나 테스트용 값으로 설정
                echo "${key}=test_value" >> "$OUTPUT_TARGET"
                ;;
        esac
    fi
done < "$ENV_EXAMPLE_FILE"

# 필수 테스트 환경 변수 추가
echo "ENVIRONMENT=test" >> "$OUTPUT_TARGET"

if [ -n "$GITHUB_ENV" ]; then
    echo "GitHub Actions 환경 변수 설정이 완료되었습니다."
else
    echo "테스트용 환경 변수가 .env.test 파일에 저장되었습니다."
    echo "생성된 파일을 확인하려면: cat .env.test"
fi

