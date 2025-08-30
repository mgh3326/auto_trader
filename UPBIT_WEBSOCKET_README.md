# 업비트 WebSocket MyOrder 자동 분석 시스템

업비트의 내 주문 및 체결 WebSocket을 이용해서 매수/매도가 발생할 때마다 `analyze_coin_json`으로 단일 코인 분석을 자동으로 수행하는 시스템입니다.

## 📋 주요 기능

- **실시간 주문/체결 모니터링**: 업비트 WebSocket MyOrder API를 통해 실시간으로 주문 및 체결 데이터 수신
- **자동 코인 분석**: 체결이 발생한 코인에 대해 자동으로 `analyze_coin_json` 함수 호출
- **재연결 기능**: 연결이 끊어져도 자동으로 재연결 시도
- **로깅 시스템**: 상세한 로그를 통한 모니터링 상태 추적

## 🚀 사용법

### 1. 기본 실행

```bash
# 모든 코인 페어 모니터링 및 자동 분석
python upbit_websocket_monitor.py
```

### 2. 테스트 실행

```bash
# WebSocket 연결 및 서비스 테스트 (실제 분석 없이)
python test_upbit_websocket.py
```

### 3. 프로그래밍 방식 사용

```python
import asyncio
from app.services.upbit_websocket import UpbitOrderAnalysisService
from app.analysis.service_analyzers import UpbitAnalyzer

async def main():
    # 분석기 초기화
    analyzer = UpbitAnalyzer()
    
    # 분석 콜백 함수 정의
    async def analyze_callback(coin_name: str):
        await analyzer.analyze_coin_json(coin_name)
    
    # 서비스 시작
    service = UpbitOrderAnalysisService(analyzer_callback=analyze_callback)
    await service.start_monitoring()
    
    # 특정 코인만 모니터링하려면:
    # await service.start_monitoring(["KRW-BTC", "KRW-ETH"])

if __name__ == "__main__":
    asyncio.run(main())
```

## 🔧 구성 요소

### 1. UpbitMyOrderWebSocket
- 업비트 WebSocket MyOrder API 클라이언트
- 실시간 주문/체결 데이터 수신
- 자동 재연결 기능

### 2. UpbitOrderAnalysisService
- 주문/체결 데이터 기반 자동 분석 서비스
- 체결 감지 시 분석 콜백 호출
- 서비스 시작/중지 관리

## 📊 모니터링 대상

- **모든 KRW 마켓 페어**: KRW-BTC, KRW-ETH, KRW-ADA 등
- **체결 상태만**: `state: "trade"` 인 데이터만 처리
- **매수/매도 구분**: ASK(매도), BID(매수) 모두 처리

## ⚙️ 설정

### 환경 변수
업비트 API 키가 `.env` 파일에 설정되어 있어야 합니다:

```env
UPBIT_ACCESS_KEY=your_upbit_access_key_here
UPBIT_SECRET_KEY=your_upbit_secret_key_here
```

### 로깅 레벨
기본적으로 `INFO` 레벨로 설정되어 있으며, 더 상세한 로그를 보려면 `DEBUG`로 변경할 수 있습니다.

## 🔄 동작 흐름

1. **WebSocket 연결**: 업비트 WebSocket MyOrder API에 연결
2. **구독 시작**: 지정된 코인 페어 또는 모든 페어 구독
3. **데이터 수신**: 실시간 주문/체결 데이터 수신
4. **체결 감지**: `state: "trade"` 상태의 데이터 필터링
5. **코인명 추출**: 페어 코드를 한국 이름으로 변환 (예: KRW-BTC → 비트코인)
6. **분석 실행**: `analyze_coin_json(coin_name)` 호출
7. **로그 기록**: 분석 시작/완료 로그 출력

## 🚨 주의사항

- **API 키 필요**: 업비트 API 키가 반드시 설정되어 있어야 합니다
- **실제 거래 데이터**: 실제 주문/체결이 발생할 때만 동작합니다
- **네트워크 연결**: 안정적인 인터넷 연결이 필요합니다
- **리소스 사용**: 분석이 자주 실행될 수 있으므로 시스템 리소스를 모니터링하세요

## 🔍 로그 예시

```
2025-01-27 10:30:15 - upbit_websocket - INFO - 업비트 MyOrder WebSocket 연결을 시작합니다...
2025-01-27 10:30:16 - upbit_websocket - INFO - WebSocket 연결 성공
2025-01-27 10:30:16 - upbit_websocket - INFO - 모든 코인 페어 구독
2025-01-27 10:30:45 - upbit_websocket - INFO - 체결 감지 - 비트코인(KRW-BTC): BID 0.001개 @ 95000000원
2025-01-27 10:30:45 - upbit_websocket - INFO - 비트코인 분석을 시작합니다...
2025-01-27 10:30:50 - upbit_websocket - INFO - 비트코인 분석이 완료되었습니다.
```

## 🛠️ 트러블슈팅

### SSL 인증서 오류 (macOS)
```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```
**해결책**: 이미 코드에서 자동으로 처리됩니다. 기본적으로 `verify_ssl=False`로 설정되어 SSL 검증을 비활성화합니다.

**수동 설정**:
```python
# SSL 검증 활성화 (권장하지 않음)
service = UpbitOrderAnalysisService(
    analyzer_callback=your_callback,
    verify_ssl=True
)
```

### WebSocket 연결 실패
- 업비트 API 키 확인
- 네트워크 연결 상태 확인
- 방화벽 설정 확인

### 분석 실행 오류
- 데이터베이스 연결 상태 확인
- Google API 키 할당량 확인
- 시스템 리소스 확인

### 재연결 문제
- 기본적으로 최대 10회 재연결 시도
- 재연결 간격: 5초
- 필요시 코드에서 `max_reconnect_attempts`, `reconnect_delay` 값 조정
