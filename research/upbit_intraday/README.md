# upbit-intraday-v1 (hk 6035)

Upbit KRW 15분 봉 장중 신호 세 계열(신규 추세 돌파·보유 코인 반등·하방 이탈)의 사전등록과 과거 재생. 연구 전용이며
주문·websocket·DB·스케줄러·정책 변경이 없다. 규칙·판정식은 `PREREG.md`, 결과는 `docs/research/6035-upbit-intraday-signals.md`.

| 파일 | 내용 |
|---|---|
| `public_http.py` | 공개 GET 허용 목록(`/v1/market/all`, `/v1/candles/minutes/15`)만. 열기 전에 URL 을 검사한다 |
| `fetch.py` | 마켓별 15분 봉을 해시된 gzip CSV 로 저장하고 `manifest.json` 을 쓴다 (재개 가능) |
| `data.py` | 해시·창을 검증하며 슬롯 패널로 읽는다. 봉 없는 슬롯은 NaN |
| `rules.py` | **동결**: 신호·결과·대조군 규칙 (manifest 가 sha256 을 핀) |
| `evaluate.py` | **동결**: 요약·부트스트랩·설정 선택·판정 (manifest 가 sha256 을 핀) |
| `run.py` | `develop`(개발 구간 선택)·`test`(시험 구간 한 번) |
| `data_manifest.json` | 수집 데이터의 해시·행 수 (원 봉 파일은 git 밖) |
| `frozen_selection.json` | 개발 구간에서 고른 설정 (시험 전에 커밋) |

```bash
ROOT=~/work/herdr-artifacts/upbit-intraday-6035            # git 밖
uv run python -m research.upbit_intraday.cli fetch   --root $ROOT
uv run python -m research.upbit_intraday.cli develop --root $ROOT --selection-out research/upbit_intraday/frozen_selection.json
uv run python -m research.upbit_intraday.cli test    --root $ROOT --selection research/upbit_intraday/frozen_selection.json   # 한 번만
uv run pytest tests/research/upbit_intraday -q
```

`rules.py`·`evaluate.py` 를 바꾸면 manifest 의 핀이 어긋나 `test` 가 거부한다(`frozen code changed`) — 새 manifest 버전이 필요하다.
