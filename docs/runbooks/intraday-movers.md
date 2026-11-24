# 장중 급등·급락 관측 로그 런북 (hk 6154)

KRX 정규장 중 네이버 상승(`up`)·하락(`down`)·거래대금(`priceTop`) 상위를 5분마다 삽입 전용으로 쌓고, 면(surface)별 성공·0건·실패를 구분해 기록한다. 최초 포착 종목은 hk 6145 분봉 수집 대상(`intraday_movers`)이 된다. 계약은 `docs/agent-contracts/market-data-and-screener-contracts.md` 「장중 급등·급락 관측 로그」.

## 1. 적용 순서 (운영자·설계 세션)

1. 코드 배포 뒤 마이그레이션 `20261124_hk6154_momentum_runs` 적용(`uv run alembic upgrade head`). 추가형(새 표 `invest_momentum_event_runs` + `at_app` SELECT·INSERT 조건부 GRANT)이고 기존 스냅샷 표는 바뀌지 않는다.
2. 쓰기 게이트 `INVEST_MOMENTUM_EVENTS_COMMIT_ENABLED=true`(기본 false)를 배포 env 에 둔다. 이 스위치는 기존 ROB-222 TaskIQ 잡(`invest_momentum_events.build`)의 쓰기 게이트이기도 하다 — TaskIQ 스케줄 스위치(`INVEST_MOMENTUM_EVENTS_SCHEDULER_ENABLED`)는 켜지 않는다.
3. 수동 확인: `python -m scripts.intraday_movers --dry-run` (네트워크만 쓰고 DB 는 쓰지 않는다). 정규장 중에 `--dry-run` 없이 1회 실행하면 면별 상태가 출력된다.
4. DAG 사본(`deploy/dagu/at-intraday-movers/`) 등록은 1–3 뒤에 설계 세션이 한다. 저장소는 등록하지 않는다.

## 2. 실행

```bash
uv run python -m scripts.intraday_movers [--dry-run]
```

- 출력 JSON: `status`(`recorded` / `dry_run` / `outside_session`), `snapshot_at`, `trading_date`, `run_id`, `surfaces[]`(`surface`, `status`, `reason`, `row_count`, `written`, `inserted_rows`, `already_recorded`, `db_error`), `failed`, (게이트가 꺼졌을 때) `commit_gate_off`.
- 종료 코드 0 = 기록·드라이런·정규장 밖, 1 = `error` 면 또는 쓰기 실패가 있음.
- 정규장 밖(09:00–15:30 KST, 끝 배타)·XKRX 휴장일이면 아무것도 가져오지 않고 쓰지 않는다.
- 같은 `snapshot_at`(초 단위)을 다시 실행해도 아무것도 바뀌지 않는다. 면의 상태 행을 먼저 넣고, 이미 있으면 그 면은 `already_recorded` 이다.

## 3. 상태 읽기

`invest_momentum_event_runs`: 한 실행(`run_id` = `snapshot_at` 에서 유도) × 면 하나가 한 행이다.

| status | reason | row_count | 뜻 |
|---|---|---|---|
| `ok` | NULL | > 0 | 순위 행을 `invest_momentum_event_snapshots` 에 기록했다 |
| `empty` | `no_rows` | 0 | 순위가 빈 목록으로 돌아왔다(조회는 성공) |
| `error` | `http_4xx` `http_5xx` `timeout` `transport` `bad_response` `unparseable` `unexpected` | 0 | 조회·해석 실패. 순위 행은 없다. 예외 문자열은 저장하지 않는다 |

DB CHECK 가 위 조합만 허용한다. 순위 행과 상태 행은 면 단위 한 트랜잭션이다.

## 4. 최초 포착 (first capture)

`app.services.invest_momentum_events.intraday.first_captures(session, trading_date, market="kr")` — 종목마다 그날 가장 이른 `snapshot_at` 의 면(`stock:KRX:ALL:<order>`)·순위·등락률·거래대금을 읽을 때 계산해 돌려준다(새 표 없음). 같은 시각 동률은 순위 → order_type → trade_type → market_type 순으로 깨서 결과가 안정적이다. 결과 순서는 `snapshot_at`, `symbol`.

## 5. 6145 분봉 수집 연동

`scripts.collect_session_minutes --market kr` 의 대상 출처 `intraday_movers` = 그날 최초 포착 종목. 우선순위는 pick log 다음, 상한에 걸리면 가장 먼저 잘린다(`targets_truncated.intraday_movers`). 이 조회가 실패하면 `error_details` 에 `origin=intraday_movers` 로 남고 실행은 계속된다.

🔴 KIS 분봉은 `n=200` 을 요청해도 **호출당 30봉**만 돌려준다(설계 세션 실측 2026-11-24). 그래서 오늘 분봉 행이 아직 없는 새 종목(`backfill`)은 첫 페이지의 가장 이른 봉 시각을 시간 커서(`FID_INPUT_HOUR_1`)로 한 번 더 읽어 이전 페이지를 붙인다 — 두 페이지 ≈ 60분. 추가 페이지가 실패하면 첫 페이지는 그대로 쓰고 오류만 보고한다. 커서 의미(`<=` 인지 `<` 인지)와 실제 소급 폭은 배포 뒤 운영 이미지로 확인한다(겹치는 봉은 `DO NOTHING`).

## 6. 범위 밖

NXT 세션(2단계), 미국 movers(3단계), DAG 등록, 설정 스위치 변경, 테마 이벤트, retro/v1, 주문·승인 경로, 과거 소급.
