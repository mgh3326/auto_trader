# Upbit 원화 1분봉 Dagu 사본 (hk 6701)

`at-upbit-1m.yaml`은 매분 24시간 실행하도록 작성한 Dagu 복사본이며 **저장소가 등록하지 않는다**. Dagu 등록은 별도 운영자 승인 대상이다.

- 진입점: `scripts.collect_upbit_minute_candles --commit`
- 대상: Upbit 보유 원화 마켓, KRW-BTC/XRP/ETH, 24시간 거래대금 상위 N개(기본 20, 최대 100)의 합집합
- 마감된 분봉만 `crypto_candles_1m`에 삽입한다. 수집기 source 행이 없는 마켓은 현재 KST 날짜를 최대 8페이지(페이지당 200봉)로 따라잡고, 이후 실행은 마지막 저장 봉 이후만 조회한다.
- 실패한 마켓을 오류로 보고하고 나머지 시장은 계속 처리한다. 실패가 있으면 종료 코드 1이다.
- 수동 실행은 `uv run python -m scripts.collect_upbit_minute_candles --dry-run`이다. 기본 드라이런은 DB 연결을 열지 않는다. `--commit`만 DB에 삽입한다.
- `UPBIT_MINUTE_CANDLE_TOP_N`은 기본 20이며 100으로 제한된다.
- WebSocket 집계는 후속 범위다.
