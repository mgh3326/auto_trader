# 세션 분봉 수집 런북 (hk 6145)

KR·US 닫힌 1분봉을 `research.kr_candles_1m` / `research.us_candles_1m` 에 세션 표지·최초 수신 시각과 함께 쌓는다. 계약은 `docs/agent-contracts/market-data-and-screener-contracts.md` 「세션 분봉 타임라인」.

## 1. 적용 순서 (운영자·설계 세션)

1. 코드 배포 뒤 마이그레이션 `20261124_hk6145_session_minutes` 적용(`uv run alembic upgrade head`). 추가형이며 `public.*_candles_1m` 은 바뀌지 않는다.
2. 환경 변수(배포 env 파일)에 고정 목록을 둔다: `SESSION_MINUTES_FIXED_SYMBOLS_US=NVDA`, `SESSION_MINUTES_FIXED_SYMBOLS_KR=005930,000660`. 선택: `SESSION_MINUTES_SCREENER_TOP_N`(기본 20), `SESSION_MINUTES_MAX_SYMBOLS_KR/US`(기본 150).
3. 수동 확인(DB 에 쓴다): `python -m scripts.collect_session_minutes --market kr --dry-run` 으로 대상·읽기 수를 확인한 뒤 `--dry-run` 없이 1회 실행.
4. DAG 사본(`deploy/dagu/at-session-minutes/`) 등록은 위 1–3 뒤에 설계 세션이 한다(결정 `decision/2026-11-24/at-session-timeline` 항목 1). 저장소는 등록하지 않는다.

## 2. 수집기

```bash
uv run python -m scripts.collect_session_minutes --market kr|us [--symbols 005930 000660] [--dry-run]
```

- 출력 JSON: `inserted`, `already_present`, `skipped_forming`, `skipped_invalid`, `skipped_value_unknown`, `errors`, `error_details`(종목·venue·예외 클래스만), `targets_by_origin`, `targets_truncated`. 종료 코드 0 = 정상, 1 = 오류 있음.
- 닫힌 봉만 쓴다(`시작 + 1분 + 10초 <= 요청 직전`). 같은 봉을 다시 받으면 `already_present` 이고 `retrieved_at` 은 최초 값 그대로다.
- `targets_truncated` 가 0 이 아니면 상한(`SESSION_MINUTES_MAX_SYMBOLS_*`)에 걸린 것이다. 낮은 순위(pick log → 스냅샷 순위 → 고정 목록 → 보유) 순으로 잘린다.
- KIS 분봉 한 번 호출은 최근 구간만 돌려준다(KR 약 30봉, US 최대 120봉). 5분 간격 DAG 에서는 겹치지만, 실행이 한동안 멈추면 그 구간은 비며 소급 채움은 없다(진단의 `missing_minutes` 로 보인다).

## 3. 커버리지 확인

```bash
uv run python -m scripts.diagnose_session_coverage --market kr|us --date 2026-11-24 [--symbols 005930]
```

읽기 전용(8초 timeout). 종목 × venue × session_segment 별 `rows`, `expected_minutes`, `missing_minutes`, `latency_p50_s`/`latency_p95_s`(= `retrieved_at − (time_utc + 1분)`). 백필·승격(`%backfill%`, `promote-%`) 배치 행은 제외한다. `UNKNOWN` 세그먼트 행은 예상 분이 0 으로 따로 나온다.

## 4. 주의

- US `session_date` 는 ET 거래일(20:00 ET 롤오버)이라 KST 달력일과 다를 수 있다.
- `research.us_candles_1m` 의 연속 집계(5m/15m/30m/1h)는 timescaledb 가 있는 DB 에서만 만들어진다. 갱신은 운영자가 명시적으로 한다(자동 refresh 정책 없음).
- 이 기능은 주문·승인·정책 경로와 무관하다. 스케줄러·systemd 등록을 저장소가 하지 않는다.
