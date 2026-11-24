# 선물 분봉 수집 런북 (hk 6148)

KOSPI200 선물·미니 KOSPI200 선물의 근월·차근월 닫힌 1분봉(주간)을 `research.kr_futures_candles_1m` 에 쌓는다. 계약은 `docs/agent-contracts/market-data-and-screener-contracts.md` 「선물 시세 읽기·분봉 수집」. 주문·원장 경로와 무관하다.

## 1. 적용 순서 (운영자·설계 세션)

1. 코드 배포 뒤 마이그레이션 `20261124_hk6148_futures_min` 적용(`uv run alembic upgrade head`). 추가형이며 다른 표는 바뀌지 않는다.
2. 수동 확인(실전 KIS 시세 읽기, 새 게이트): `KIS_FUTURES_QUOTES_ENABLED=true uv run python -m scripts.collect_futures_minutes --dry-run` 으로 `products_resolved`(2)·`contracts`·`would_write` 를 확인한 뒤 `--dry-run` 없이 1회 실행. 게이트 값은 그 한 번의 실행에만 둔다(공유 env 파일에 넣지 않는다).
3. 스모크 확인 항목(`[unverified]`): ① 미니 선물 단축코드(`A05…`)와 시장 코드 `F` 가 시세·분봉 TR 에서 받아들여지는지 ② `stck_cntg_hour` 가 봉 시작 라벨인지(거래소 화면의 한 봉과 대조) ③ `cntg_vol` 이 분 단위 거래량인지 ④ 한 번에 최대 102행이라 주간 전체(420분)가 5페이지 안에 받아지는지. 어긋나면 수집기를 켜지 말고 설계 세션에 알린다.
4. DAG 사본(`deploy/dagu/at-futures-minutes/`) 등록은 위 1–3 뒤에 **별도 운영자 승인**으로만 한다. 저장소는 등록하지 않는다.

## 2. 수집기

```bash
KIS_FUTURES_QUOTES_ENABLED=true uv run python -m scripts.collect_futures_minutes [--dry-run]
```

- 출력 JSON: `inserted`, `already_present`, `skipped_forming`, `skipped_invalid`, `products_resolved`, `contracts`, `night`, `errors`, `error_details`. 종료 코드 0 = 정상, 1 = 오류 있음, 2 = 게이트 꺼짐.
- `night` 는 항상 `"unsupported"` 다 — KIS 공식 샘플에 야간 시세·분봉 TR 이 없다. 야간 DAG 사본을 등록해도 새 행은 없다.
- 닫힌 봉만 쓰고(`시작 + 1분 + 10초 <= 요청 직전`) 재실행은 `already_present` 이며 `retrieved_at` 은 최초 값이다. 소급 채움은 없다.
- 범위 안 `UNKNOWN`: 달력이 확인하지 못한 날의 봉은 `session_segment=UNKNOWN`, `session_date` NULL 로 저장된다(추측 없음).
- 최종거래일 주에는 만기 계약이 `roll_state=expiring` 이다. 마스터의 월 목록과 달력 범위(2026-11-23~12-30) 밖의 최종거래일은 휴장 보정 없는 둘째 목요일로 계산한다.

## 3. 커버리지 확인

```bash
uv run python -m scripts.diagnose_session_coverage --market futures --date 2026-11-24 [--symbols MF200.2612]
```

읽기 전용(8초 timeout). 계약 × `session_segment` 별 `rows`, `expected_minutes`(주간 08:45–15:45 중 이미 닫힌 분), `missing_minutes`, `latency_p50_s`/`latency_p95_s`. 야간 행이 있으면 예상 0 으로 따로 나오고 `night_collection: "unsupported"` 가 붙는다.

## 4. 주의

- 야간 봉의 `session_date` 는 야간이 시작된 날의 **다음 XKRX 거래일**이다(금요일 야간 → 월요일). 주간 봉은 KST 날짜다.
- 이 기능은 주문·승인·원장·정책 경로와 무관하고 `kis_futures_mock` 동작을 바꾸지 않는다(상수·달력은 읽기만). 스케줄러·systemd 등록을 저장소가 하지 않는다.
