# Upbit 원화장 장중 그림자 기록 DAG 복사본 (hk 6036)

`at-upbit-intraday-shadow.yaml` 은 **저장소에서 등록되지 않는 복사본(copy, NOT registered)**이다(하드룰 6). 이 PR/저장소는 Dagu 에 복사하거나 등록하지 않는다.
등록은 hk 6036 머지와 마이그레이션 `20261125_hk6036_upbit_shadow` 적용 뒤에 설계 세션이 한다.

| DAG | 스케줄 | 진입점 |
|---|---|---|
| `at-upbit-intraday-shadow` | 매시 01·16·31·46분 (KST, 24시간 7일) | `scripts.upbit_intraday_shadow --commit` |

- 주문·미리보기·원장 쓰기가 없다. 공개 마켓 목록과 15분 봉만 읽고 `research.upbit_shadow_candidates`·`research.upbit_shadow_virtual_trades` 에 삽입 전용으로 쓴다.
- 손으로 볼 때는 `uv run python -m scripts.upbit_intraday_shadow [--markets KRW-BTC,KRW-XRP]` — 기본은 드라이런이고 DB 를 읽지도 쓰지도 않는다.
- 한 회차는 최근 마감 봉 4개(1시간)를 평가한다. 신호 시각보다 30분 넘게 늦게 본 경보 상태는 그 시각의 증거가 아니므로 `market_event_unknown` 으로 제외 기록한다.
- 빠진 회차가 4개를 넘으면 그 사이의 봉은 기록되지 않는다. 필요하면 `--catchup-bars`(최대 96)로 다시 돌린다.
- 시장 하나라도 실패하면 종료 코드 1 이다. 아직 판정할 수 없는 가상 거래는 행을 만들지 않고 다음 회차에 다시 본다.
