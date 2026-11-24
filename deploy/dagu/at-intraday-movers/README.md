# 장중 급등·급락 관측 DAG 복사본 (hk 6154)

`at-intraday-movers.yaml` 은 **저장소에서 등록되지 않는 복사본(copy, NOT registered)**이다(하드룰 6). 이 PR/저장소는 Dagu 에 복사하거나 등록하지 않는다.
등록은 hk 6154 머지, 마이그레이션 `20261124_hk6154_momentum_runs` 적용, 그리고 `INVEST_MOMENTUM_EVENTS_COMMIT_ENABLED=true` 설정 뒤에 설계 세션이 한다.

| DAG | 스케줄 | 진입점 |
|---|---|---|
| `at-intraday-movers` | 월–금 09:00–15:25 KST, 5분 간격 | `scripts.intraday_movers` |

- 정규장(09:00–15:30 KST, 끝 배타)·XKRX 거래일 밖이면 스크립트가 `outside_session` 으로 끝나고 아무것도 쓰지 않는다. 공휴일에도 DAG 는 돌지만 쓰기는 없다.
- 면(surface)이 `error` 이면 종료 코드가 1 이라 DAG 실행이 실패로 남는다. `empty` 는 실패가 아니다.
- 쓰기 게이트가 꺼져 있으면(`commit_gate_off`) 드라이런으로 끝난다.
- `host: pi` 워커, 배포 이미지 digest, `/root/at-secrets/.env.api` 는 at-session-minutes 와 같은 패턴이다.
- 운영 절차는 `docs/runbooks/intraday-movers.md`.
