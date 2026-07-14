# 업비트 WebSocket MyOrder 모니터링

업비트의 내 주문 및 체결 WebSocket을 소비하는 경로는 두 가지입니다. 운영 체결 경로는
`websocket_monitor.py --mode upbit`이며, 체결 레저 기록·주문 제안 런(proposal rung) 상태
반영·Discord/Telegram 알림을 담당합니다. `upbit_websocket_monitor.py`는 분석기가 제거된
레거시 콜백 모니터이며, 현재 분석 콜백은 로그만 남기고 실제 분석을 실행하지 않습니다.

## 📋 주요 기능

- **실시간 주문/체결 모니터링**: 업비트 WebSocket MyOrder API를 통해 실시간으로 주문 및 체결 데이터 수신
- **체결 증거 기록**: `trade` 이벤트의 건별 체결량을 `review.execution_ledger`에 멱등 기록
- **주문 제안 상태 반영**: 업비트 UUID와 `identifier`로 런을 찾아 `trade`는 부분 체결,
  `done`은 최종 체결로 누적 수량을 반영. 프로젝션 실패는 체결 레저를 롤백하지 않고 로그로 남김
- **체결 알림**: 새 체결을 Discord/Telegram으로 전송. 제안 런이 매칭된 체결은 일반
  소액 알림 임계치를 넘지 않아도 전송하며, 중복 레저 이벤트는 중복 알림을 만들지 않음
- **운영 건강성 로그**: 수신 메시지/체결 이벤트 수, 마지막 수신/체결 시각,
  알림 전송 수를 통합 건강성 로그에 표시
- **재연결 기능**: 연결이 끊어져도 자동으로 재연결 시도
- **로깅 시스템**: 상세한 로그를 통한 모니터링 상태 추적

## 🚀 사용법

### 1. 기본 실행

```bash
# 운영 체결 레저/제안 런/알림 모니터
uv run python websocket_monitor.py --mode upbit

# 레거시 no-op 분석 콜백 모니터
python upbit_websocket_monitor.py
```

### 2. 테스트 실행

```bash
# 외부 WebSocket/브로커를 사용하지 않는 서비스·운영 경로 테스트
uv run pytest --no-cov \
  tests/test_upbit_websocket_service.py \
  tests/test_websocket_monitor.py -q
```

### 3. 프로그래밍 방식 사용

```python
import asyncio
from websocket_monitor import UnifiedWebSocketMonitor

async def main():
    monitor = UnifiedWebSocketMonitor(mode="upbit")
    await monitor.start()

if __name__ == "__main__":
    asyncio.run(main())
```

## 🔧 구성 요소

### 1. UpbitMyOrderWebSocket
- 업비트 WebSocket MyOrder API 클라이언트
- 실시간 주문/체결 데이터 수신
- 자동 재연결 기능

### 2. UnifiedWebSocketMonitor
- Upbit/KIS 체결 소비자 오케스트레이션
- execution ledger 커밋 후 제안 런 프로젝션
- 알림, 심박 파일, 재연결, 건강성 로그 관리

### 3. UpbitOrderAnalysisService (레거시)
- `upbit_websocket_monitor.py`가 사용하는 콜백 래퍼
- 현재 엔트리포인트의 분석 콜백은 no-op

## 📊 모니터링 대상

- **모든 KRW 마켓 페어**: KRW-BTC, KRW-ETH, KRW-ADA 등
- **운영 체결 상태**: `state: "trade"`와 `state: "done"` 처리. `done`은 같은 UUID의
  영속 체결 레저 증거가 있을 때만 제안 런을 최종 체결로 종료
- **레거시 콜백 상태**: `upbit_websocket_monitor.py`는 `state: "trade"`만 콜백으로 전달
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
2. **데이터 수신**: 수신 수와 마지막 메시지 시각을 즉시 갱신
3. **`trade` 체결 기록**: 건별 `volume`을 execution ledger에 먼저 커밋
4. **제안 런 프로젝션**: 별도 DB 세션에서 누적 `executed_volume`을 부분 체결로 반영
5. **`done` 종료 반영**: 같은 주문의 커밋된 체결이 확인되면 누적 수량으로 최종 체결 처리
6. **알림 전송**: 중복을 제외한 체결을 전송하고, 매칭된 제안 런 체결은 소액 임계치 면제
7. **건강성 기록**: `messages_received`, `execution_events_received`, `fills_forwarded`,
   `last_message_at`, `last_execution_at`을 주기적으로 로그

## 🚀 운영 배포

macOS 네이티브 배포의 업비트 소비자는
`com.robinco.auto-trader.upbit-websocket` 단일 활성 launchd 서비스입니다. API/MCP처럼
블루/그린으로 교체되지 않으므로 코드 배포 후 재시작해야 합니다.

```bash
# 표준 네이티브 배포: current 심볼릭 전환 후 서비스를 자동 재시작
scripts/deploy-native.sh <commit-sha>

# 수동/아웃오브밴드 배포 후 재시작 확인
launchctl kickstart -k gui/$(id -u)/com.robinco.auto-trader.upbit-websocket
```

## 🚨 주의사항

- **API 키 필요**: 업비트 API 키가 반드시 설정되어 있어야 합니다
- **실제 거래 데이터**: 실제 주문/체결이 발생할 때만 동작합니다
- **네트워크 연결**: 안정적인 인터넷 연결이 필요합니다
- **데이터베이스 연결**: `done` 종료 반영과 제안 런 프로젝션은 영속 체결 증거를 사용합니다

## 🔍 로그 예시

```
2026-07-14 10:30:45 - websocket_monitor - INFO - Execution ledger websocket upsert committed: broker=upbit ...
2026-07-14 10:30:45 - websocket_monitor - INFO - Upbit proposal rung projected: order_id=... state=trade rung_state=partially_filled ...
2026-07-14 10:35:00 - websocket_monitor - INFO - Unified WebSocket health: mode=upbit connected=True ... messages_received=12 execution_events_received=3 fills_forwarded=3 ...
```

## 🛠️ 트러블슈팅

### SSL 인증서 오류 (macOS)
```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate
```
**해결책**: SSL 검증은 항상 활성화됩니다. 로컬 신뢰 저장소를 정리한 뒤 다시 연결하세요.

- python.org 설치본을 쓰는 경우 `/Applications/Python 3.x/Install Certificates.command`를 실행합니다.
- macOS 키체인 또는 사내 프록시 환경에서 필요한 루트/중간 인증서를 신뢰하도록 추가합니다.
- 시스템 시간이 크게 틀어지지 않았는지 확인합니다.

### WebSocket 연결 실패
- 업비트 API 키 확인
- 네트워크 연결 상태 확인
- 방화벽 설정 확인

### 제안 런 프로젝션 오류
- `Upbit proposal rung projection failed` 로그와 데이터베이스 연결 상태 확인
- 체결 레저 커밋은 유지되므로 이후 reconcile에서 상태가 수렴하는지 확인
- `done` 스킵이 반복되면 같은 Upbit UUID의 체결 레저 행이 존재하는지 확인

### 재연결 문제
- 통합 모니터는 연결 종료 후 슈퍼바이저 루프에서 재연결
- 기본 재연결 간격: 5초
- 필요 시 `WS_MONITOR_RECONNECT_DELAY_SECONDS`로 운영 재연결 간격 조정
