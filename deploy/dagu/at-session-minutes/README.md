# 세션 분봉 수집 DAG 복사본 (hk 6145)

아래 두 DAG 파일은 **저장소에서 등록되지 않는 복사본**이다(하드룰 6). 이 PR/저장소는 Dagu 에 복사하거나 등록하지 않는다.
등록은 결정 `decision/2026-11-24/at-session-timeline` 항목 1에 따라 **hk 6145 머지와 research 마이그레이션 적용 뒤** 설계 세션이 한다.

| DAG | 스케줄 | 진입점 |
|---|---|---|
| `at-session-minutes-kr` | 월–금 08:00–20:10 KST, 5분 간격 | `scripts.collect_session_minutes --market kr` |
| `at-session-minutes-us` | 월–금 04:00–20:10 ET + 데이마켓 창(일–금 20:00–23:55 ET, 월–토 00:00–03:55 ET), 5분 간격 (hk 6322) | `scripts.collect_session_minutes --market us` |

- US 스케줄은 ET 기준이다. KST 로는 여름(EDT) 17:00–09:10, 겨울(EST) 18:00–10:10 이다. KST 18:00 고정 시작은 여름 프리장 첫 1시간(04:00–05:00 ET)을 놓친다.
- 두 DAG 모두 `host: pi` 워커, 배포 이미지 digest, `/root/at-secrets/.env.api`를 쓴다(at-batch2 와 같은 패턴).
- 수집 결과 JSON 의 `errors` 가 0 이 아니면 종료 코드가 0 이 아니어서 DAG 실행이 실패로 남는다.
- 고정 수집 목록은 환경 변수 `SESSION_MINUTES_FIXED_SYMBOLS_KR`, `SESSION_MINUTES_FIXED_SYMBOLS_US`(쉼표 구분, 기본 빈 값)로 둔다. 초기값은 운영자가 NVDA / 005930,000660 으로 정한다.
- 운영 절차와 커버리지 확인은 `docs/runbooks/session-minutes.md`.
