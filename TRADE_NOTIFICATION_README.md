# 거래 알림 시스템 (Trade Notification System)

Telegram을 통해 거래 이벤트(매수/매도/분석 완료)를 실시간으로 알림받는 시스템입니다.

## 목차

- [개요](#개요)
- [설정 방법](#설정-방법)
- [알림 유형](#알림-유형)
- [사용 예제](#사용-예제)
- [테스트](#테스트)
- [문제 해결](#문제-해결)

## 개요

### 주요 기능

- **실시간 거래 알림**: 매수/매도 주문 체결 시 즉시 알림
- **AI 분석 결과 알림**: 분석 완료 시 투자 판단 및 신뢰도 정보 제공
- **주문 취소 알림**: 미체결 주문 취소 시 알림
- **자동화 요약 알림**: 전체 자동 거래 실행 결과 요약
- **다중 채팅방 지원**: 여러 텔레그램 채팅방에 동시 전송
- **마크다운 포맷팅**: 가독성 높은 메시지 형식

### 아키텍처

```
app/monitoring/trade_notifier.py   # TradeNotifier 싱글톤 클래스
app/main.py                         # 앱 시작 시 초기화
app/tasks/analyze.py                # Celery 태스크에서 알림 전송
```

## 설정 방법

### 1. 환경 변수 설정

`.env` 파일에 다음 변수를 설정합니다:

```bash
# Telegram 봇 토큰 (필수)
TELEGRAM_TOKEN=your_telegram_bot_token

# Telegram 채팅방 ID (필수)
TELEGRAM_CHAT_ID=123456789

# 에러 알림용 채팅방 ID (선택사항, 미설정 시 TELEGRAM_CHAT_ID 사용)
ERROR_REPORTING_CHAT_ID=123456789

# 에러 알림 활성화 (선택사항, 기본값: false)
ERROR_REPORTING_ENABLED=true

# 중복 에러 필터링 시간 (초, 선택사항, 기본값: 300)
ERROR_DUPLICATE_WINDOW=300
```

### 2. Telegram 봇 생성

1. Telegram에서 [@BotFather](https://t.me/botfather) 검색
2. `/newbot` 명령으로 새 봇 생성
3. 봇 이름과 username 설정
4. 발급받은 토큰을 `TELEGRAM_TOKEN`에 설정

### 3. 채팅방 ID 확인

1. 봇을 채팅방에 추가
2. 봇에게 메시지 전송
3. 브라우저에서 다음 URL 접속:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
4. 응답에서 `chat.id` 값을 확인하여 `TELEGRAM_CHAT_ID`에 설정

### 4. 앱 시작

```bash
# 개발 서버 시작
make dev

# 또는
uv run uvicorn app.main:app --reload
```

앱 시작 시 로그에서 초기화 확인:
```
INFO:app.main:Trade notifier initialized: chat_id=123456789
```

## 알림 유형

### 1. 매수 주문 알림

```
💰 매수 주문 체결
🕒 2025-01-08 12:34:56

종목: 비트코인 (BTC)
시장: 암호화폐
주문 수: 3건
총 금액: 300,000원

매수 가격대:
  1. 100,000원
  2. 101,000원
  3. 102,000원
```

**발생 조건**:
- `process_buy_orders_with_analysis` 함수 실행 성공
- 1건 이상의 매수 주문 체결

**관련 함수**: `app/tasks/analyze.py:_execute_buy_order_for_coin_async`

### 2. 매도 주문 알림

```
💸 매도 주문 체결
🕒 2025-01-08 12:35:30

종목: 이더리움 (ETH)
시장: 암호화폐
주문 수: 2건
총 수량: 0.5
예상 금액: 1,025,000원

매도 가격대:
  1. 2,000,000원
  2. 2,100,000원
```

**발생 조건**:
- `place_multiple_sell_orders` 함수 실행 성공
- 1건 이상의 매도 주문 체결

**관련 함수**: `app/tasks/analyze.py:_execute_sell_order_for_coin_async`

### 3. AI 분석 완료 알림

```
🟢 AI 분석 완료
🕒 2025-01-08 12:30:00

종목: 비트코인 (BTC)
시장: 암호화폐
판단: 매수
신뢰도: 85.5%

주요 근거:
  1. 상승 추세 지속
  2. 거래량 증가
  3. 기술적 지표 긍정적
```

**판단 유형**:
- 🟢 매수 (buy)
- 🟡 보유 (hold)
- 🔴 매도 (sell)

**발생 조건**:
- `UpbitAnalyzer.analyze_coin_json` 실행 성공
- 구조화된 JSON 분석 결과 반환

**관련 함수**: `app/tasks/analyze.py:_analyze_coin_async`

### 4. 주문 취소 알림

```
🚫 주문 취소
🕒 2025-01-08 12:40:00

종목: 리플 (XRP)
시장: 암호화폐
취소 유형: 매수
취소 건수: 5건
```

**발생 조건**: 수동으로 호출 시 (현재 자동 호출 미구현)

**사용 예제**:
```python
from app.monitoring.trade_notifier import get_trade_notifier

notifier = get_trade_notifier()
await notifier.notify_cancel_orders(
    symbol="XRP",
    korean_name="리플",
    cancel_count=5,
    order_type="매수",
    market_type="암호화폐",
)
```

### 5. 자동화 요약 알림

```
🤖 자동 거래 실행 완료
🕒 2025-01-08 13:00:00

처리 종목: 10개
분석 완료: 10개
매수 주문: 3건
매도 주문: 2건
실행 시간: 45.5초
```

**발생 조건**: 수동으로 호출 시 (배치 작업 완료 시 사용 권장)

**사용 예제**:
```python
from app.monitoring.trade_notifier import get_trade_notifier

notifier = get_trade_notifier()
await notifier.notify_automation_summary(
    total_coins=10,
    analyzed=10,
    bought=3,
    sold=2,
    errors=0,
    duration_seconds=45.5,
)
```

## 사용 예제

### 기본 사용법

```python
from app.monitoring.trade_notifier import get_trade_notifier

# 싱글톤 인스턴스 가져오기
notifier = get_trade_notifier()

# 매수 주문 알림
await notifier.notify_buy_order(
    symbol="BTC",
    korean_name="비트코인",
    order_count=3,
    total_amount=300000.0,
    prices=[100000.0, 101000.0, 102000.0],
    volumes=[0.001, 0.001, 0.001],
    market_type="암호화폐",
)

# 매도 주문 알림
await notifier.notify_sell_order(
    symbol="ETH",
    korean_name="이더리움",
    order_count=2,
    total_volume=0.5,
    prices=[2000000.0, 2100000.0],
    volumes=[0.25, 0.25],
    expected_amount=1025000.0,
    market_type="암호화폐",
)

# AI 분석 완료 알림
await notifier.notify_analysis_complete(
    symbol="BTC",
    korean_name="비트코인",
    decision="buy",
    confidence=85.5,
    reasons=["상승 추세 지속", "거래량 증가"],
    market_type="암호화폐",
)
```

### 연결 테스트

```python
from app.monitoring.trade_notifier import get_trade_notifier

notifier = get_trade_notifier()
success = await notifier.test_connection()

if success:
    print("텔레그램 연결 성공!")
else:
    print("텔레그램 연결 실패. 설정을 확인하세요.")
```

### Celery 태스크에서 사용

```python
from celery import shared_task
from app.monitoring.trade_notifier import get_trade_notifier

@shared_task
def my_trading_task():
    # 거래 로직 실행
    result = execute_trade()

    # 결과를 텔레그램으로 알림
    notifier = get_trade_notifier()
    asyncio.run(notifier.notify_buy_order(
        symbol=result["symbol"],
        korean_name=result["name"],
        order_count=result["orders"],
        total_amount=result["amount"],
        prices=[],
        volumes=[],
    ))
```

## 테스트

### 단위 테스트 실행

```bash
# TradeNotifier 테스트만 실행
uv run pytest tests/test_trade_notifier.py -v

# 전체 테스트 실행
make test

# 커버리지 포함 테스트
make test-cov
```

### 통합 테스트 (실제 Telegram API 호출)

```bash
# 환경 변수 설정 후
python -c "
import asyncio
from app.monitoring.trade_notifier import TradeNotifier

async def test():
    notifier = TradeNotifier()
    notifier.configure(
        bot_token='YOUR_TOKEN',
        chat_ids=['YOUR_CHAT_ID'],
        enabled=True,
    )
    result = await notifier.test_connection()
    print('Success!' if result else 'Failed')
    await notifier.shutdown()

asyncio.run(test())
"
```

## 문제 해결

### 알림이 전송되지 않음

**원인 1**: 환경 변수 미설정
```bash
# .env 파일 확인
cat .env | grep TELEGRAM
```

**해결**: `TELEGRAM_TOKEN`과 `TELEGRAM_CHAT_ID` 설정

**원인 2**: TradeNotifier 비활성화
```python
# app/main.py 로그 확인
# "Trade notifier is disabled" 메시지가 있는지 확인
```

**해결**: 앱 재시작 또는 환경 변수 확인

**원인 3**: 봇이 채팅방에 없음
```
Error: {"error_code": 403, "description": "Forbidden: bot is not a member of the group chat"}
```

**해결**: 봇을 채팅방에 추가하고 관리자 권한 부여

### 에러 알림만 오고 거래 알림은 안옴

**원인**: `ErrorReporter`는 설정되었지만 `TradeNotifier`는 비활성화됨

**해결**:
1. `TELEGRAM_CHAT_ID` 환경 변수 확인
2. 앱 재시작
3. 로그에서 "Trade notifier initialized" 메시지 확인

### 알림이 전송되지 않음 (봇 관련)

**원인**: 채팅방 ID가 잘못되었거나 봇이 해당 채팅방에 없음

**해결**:
1. 채팅방 ID 확인
2. 봇이 채팅방에 추가되었는지 확인
3. 로그에서 에러 메시지 확인

### 메시지 포맷이 깨짐

**원인**: Markdown 특수 문자 이스케이프 문제

**해결**:
- 심볼이나 이름에 `_`, `*`, `[`, `]` 등 특수 문자가 있는 경우
- `TradeNotifier`의 메시지 포맷 함수에서 자동으로 처리됨
- 추가 이스케이프가 필요한 경우 이슈 리포트

## API Reference

### TradeNotifier

#### 초기화

```python
notifier = TradeNotifier()  # 싱글톤
notifier.configure(
    bot_token: str,           # Telegram 봇 토큰
    chat_ids: List[str],      # 채팅방 ID 리스트
    enabled: bool = True,     # 활성화 여부
)
```

#### 메서드

##### notify_buy_order
```python
await notifier.notify_buy_order(
    symbol: str,              # 거래 심볼 (예: "BTC")
    korean_name: str,         # 한글 이름 (예: "비트코인")
    order_count: int,         # 주문 건수
    total_amount: float,      # 총 금액 (원)
    prices: List[float],      # 주문 가격 리스트
    volumes: List[float],     # 주문 수량 리스트
    market_type: str = "암호화폐",  # 시장 유형
) -> bool  # 전송 성공 여부
```

##### notify_sell_order
```python
await notifier.notify_sell_order(
    symbol: str,
    korean_name: str,
    order_count: int,
    total_volume: float,      # 총 수량
    prices: List[float],
    volumes: List[float],
    expected_amount: float,   # 예상 금액 (원)
    market_type: str = "암호화폐",
) -> bool
```

##### notify_analysis_complete
```python
await notifier.notify_analysis_complete(
    symbol: str,
    korean_name: str,
    decision: str,            # "buy", "hold", "sell"
    confidence: float,        # 0-100
    reasons: List[str],       # 판단 근거 (최대 3개)
    market_type: str = "암호화폐",
) -> bool
```

##### notify_cancel_orders
```python
await notifier.notify_cancel_orders(
    symbol: str,
    korean_name: str,
    cancel_count: int,        # 취소 건수
    order_type: str = "전체",  # "매수", "매도", "전체"
    market_type: str = "암호화폐",
) -> bool
```

##### notify_automation_summary
```python
await notifier.notify_automation_summary(
    total_coins: int,         # 처리된 종목 수
    analyzed: int,            # 분석 완료 수
    bought: int,              # 매수 주문 수
    sold: int,                # 매도 주문 수
    errors: int,              # 에러 발생 수
    duration_seconds: float,  # 실행 시간 (초)
) -> bool
```

##### test_connection
```python
await notifier.test_connection() -> bool  # 연결 테스트
```

##### shutdown
```python
await notifier.shutdown()  # HTTP 클라이언트 종료
```

## 향후 개선 사항

- [ ] KIS 국내/해외 주식 거래 알림 추가
- [ ] 주문 취소 알림 자동 호출
- [ ] 배치 작업 완료 시 자동화 요약 알림
- [ ] 알림 설정 ON/OFF 웹 UI
- [ ] 알림 레벨 설정 (모두/중요만/에러만)
- [ ] 알림 메시지 템플릿 커스터마이징
- [ ] 주문 체결 확인 알림 (미체결 → 체결 완료 시)
- [ ] 가격 변동 알림 (목표가 도달 시)

## 관련 문서

- [MONITORING_README.md](./MONITORING_README.md) - 에러 알림 시스템
- [CLAUDE.md](./CLAUDE.md) - 프로젝트 전체 가이드
- [app/monitoring/trade_notifier.py](./app/monitoring/trade_notifier.py) - 소스 코드
