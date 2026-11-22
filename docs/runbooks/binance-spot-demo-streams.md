# Binance Spot Demo 읽기 전용 스트림 수집기 (hk 6067)

계약: `docs/agent-contracts/broker-and-ledger-contracts.md` "Binance Spot Demo 읽기 전용 스트림 수집기".
설계: `docs/design/6033-mock-venue-streams.md` (K4, D-1, P5–P7).

## 실행 (수동, foreground)

```bash
BINANCE_SPOT_DEMO_STREAMS_ENABLED=true \
  uv run python -m scripts.binance_spot_demo_streams \
  --symbols BTCUSDT,ETHUSDT --record-dir <레포 밖 디렉터리> --duration-s 600
```

- 시세만: `--no-user-stream` (자격증명 불필요).
- 사용자 스트림: `BINANCE_SPOT_DEMO_API_KEY`/`_SECRET`(없으면 `BINANCE_DEMO_API_*`) 이름의 env. 값을 출력하지 않는다.
- 종료 코드 2 = 게이트 off·자격증명/심볼 오류. 마지막 줄에 통계(frames·reconnects·redis_errors 등)가 나온다.

## 읽기

```bash
redis-cli XREVRANGE book:binance_spot_demo:BTCUSDT + - COUNT 3
redis-cli XREVRANGE fills:binance_spot_demo + - COUNT 3
```

## 알려진 미검증 ([unverified], P7 스모크가 확인)

1. `userDataStream.subscribe.signature` 가 HMAC 키를 받는지, 데모 호스트에서 동작하는지. 거절되면 `subscribe_errors` 가 오르고 시세는 계속 수집된다. 필요하면 Ed25519 `session.logon` + `userDataStream.subscribe` 는 별도 결정이다.
2. 데모 `@depth20` 이 live 와 같은 호가인지(P5), 터치/관통 체결 규칙(P6).

## 하지 않는 것

주문·취소·원장 쓰기·스케줄러·systemd 활성화·호스트 override. 유닛 파일이 필요하면 제안만 하고 등록하지 않는다.
