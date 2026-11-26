# 장중 급등·급락 관측 DAG 복사본 (hk 6154, hk 6510, hk 6758)

아래 두 파일은 **저장소에서 등록되지 않는 복사본(copy, NOT registered)**이다(하드룰 6). 이 PR/저장소는 Dagu 에 복사하거나 등록하지 않는다.
등록은 마이그레이션 적용(`20261124_hk6154_momentum_runs`, `20261125_hk6510_us_movers` + `_val`, `20261126_hk6758_kr_mover_source` + `_val`)과
`INVEST_MOMENTUM_EVENTS_COMMIT_ENABLED=true` 설정 뒤에 설계 세션이 한다.

| DAG | 스케줄 | 진입점 |
|---|---|---|
| `at-intraday-movers` | 월–금 NXT 장전 08:00–08:45, KRX 정규장 09:00–15:25, NXT·KRX 장후 16:00–19:55 KST, 5분 간격 | `scripts.intraday_movers` |
| `at-us-intraday-movers` | 월–금 04:00–20:10 ET, 5분 간격 | `scripts.us_intraday_movers` |

- KR 스크립트는 XKRX 거래일의 NXT 장전 `[08:00, 08:50)`, KRX 정규장 `[09:00, 15:30)`, NXT·KRX 장후 `[16:00, 20:00)`에서만 수집한다. 그 밖은 `outside_session` 으로 끝나고 아무것도 쓰지 않는다. 공휴일에도 DAG 는 돌지만 쓰기는 없다.
- US 스크립트는 04:00–20:00 ET 창 안의 XNYS 거래일에서만 쓰고, 그 밖은 `outside_session` 이다. 세션 라벨은 `us_pre`/`us_regular`/`us_post` 이고 면(surface)은 `us:<source>:<session>:<side>` 다.
- 면(surface)이 `error` 이면 종료 코드가 1 이라 DAG 실행이 실패로 남는다. `empty` 는 실패가 아니다. US 에서 소스 자격증명이 없으면 그 소스의 면은 `error`/`source_unavailable` 이다.
- 쓰기 게이트가 꺼져 있으면(`commit_gate_off`) 드라이런으로 끝난다.
- US DAG 은 Alpaca paper-data 키(`ALPACA_DATA_API_KEY_ID`/`ALPACA_DATA_API_SECRET_KEY`)와 KIS 키를 `.env.api` 에서 읽는다. 키 값은 출력하지 않는다.
- `host: pi` 워커, 배포 이미지 digest, `/root/at-secrets/.env.api` 는 at-session-minutes 와 같은 패턴이다.
- 운영 절차는 `docs/runbooks/intraday-movers.md`.
