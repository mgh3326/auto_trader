# 선물 분봉 수집 DAG 복사본 (hk 6148)

아래 두 DAG 파일은 **저장소에서 등록되지 않는 복사본**이다(하드룰 6). 이 PR/저장소는 Dagu 에 복사하거나 등록하지 않는다.
등록은 **hk 6148 머지와 research 마이그레이션 적용 뒤, 별도 운영자 승인**으로만 한다(결정 `decision/2026-11-24/at-session-timeline` 항목 2·3 범위 밖).

| DAG | 스케줄 | 진입점 |
|---|---|---|
| `at-futures-minutes-day` | 월–금 08:40–15:50 KST, 5분 간격 | `scripts.collect_futures_minutes` |
| `at-futures-minutes-night` | 월–금 17:55 ~ 익일 06:10 KST, 5분 간격 | `scripts.collect_futures_minutes` |

- 두 DAG 모두 `host: pi` 워커, 배포 이미지 digest, `/root/at-secrets/.env.api`를 쓰며 `KIS_FUTURES_QUOTES_ENABLED=true` 는 이 컨테이너 실행에만 둔다(기본 off, 공유 env 파일에 넣지 않는다).
- 야간 시세·분봉 TR 이 KIS 공식 샘플에 없어 수집기는 야간을 `night: "unsupported"` 로 보고한다. 야간 DAG 는 TR 이 문서화되기 전까지 새 행을 만들지 않는다.
- 결과 JSON 의 `errors` 가 0 이 아니면 종료 코드가 0 이 아니어서 DAG 실행이 실패로 남는다. 게이트가 꺼져 있으면 종료 코드 2 다.
- 운영 절차와 커버리지 확인은 `docs/runbooks/futures-minutes.md`.
