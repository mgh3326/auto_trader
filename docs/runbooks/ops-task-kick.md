# NCP TaskIQ task-kick API — 은퇴 (hk 5753)

이 런북의 HTTP 표면은 제거됐다. `POST /trading/api/ops/tasks/{task}/kick`,
`GET /trading/api/ops/tasks/runs/{id}`, `app/services/ops_task_kick/`,
`app/routers/ops_task_kick.py`, 그리고 미들웨어의 `X-Ops-Task-Token` 분기와
`OPS_TASK_KICK_TOKEN*` 설정이 모두 사라졌다.

근거: TaskIQ worker 를 은퇴시켰다(운영자 결정 `decision/2026-11-20/at-direction-daily-plan-nonprice-go` 항목 7).
레포 안에는 이 표면을 호출하는 live 프롬프트·lane allowlist 가 없었고,
`docs/runbooks/prefect-to-postuntil.md` 기준 kick 타이머도 연결돼 있지 않았다.

- 이 경로로 들어오던 요청은 이제 일반 세션 인증을 거치며 `404`/`401` 로 끝난다.
- 외부 자동화(Prefect·postuntil 타이머)가 아직 남아 있다면 배포 전에 운영자가 호스트에서 확인해야 한다.
  레포는 그런 호출자를 알지 못한다.
- 대체 경로가 필요하면 작업 본문을 `python -m scripts.X` one-shot 으로 옮기는 것이 원칙이다
  (스케줄 등록은 운영자 몫).
